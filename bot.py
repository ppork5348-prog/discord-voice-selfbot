import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import discord

REJOIN_SECONDS = 5
ROOT = Path(__file__).resolve().parent

def load_env(path: str = ".env") -> None:
    env_path = ROOT / path
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        if "=" not in trimmed:
            continue

        key, value = trimmed.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if key not in os.environ:
            os.environ[key] = value

def load_file_tokens() -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()

    env_tokens = os.environ.get("TOKENS", "").strip()
    if env_tokens:
        for part in env_tokens.replace(",", "\n").splitlines():
            token = part.strip()
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)

    tokens_path = ROOT / "tokens.txt"

    if tokens_path.exists():
        for line in tokens_path.read_text(encoding="utf-8").splitlines():
            token = line.strip()
            if not token or token.startswith("#"):
                continue
            if token not in seen:
                seen.add(token)
                tokens.append(token)

    return tokens

def load_accounts() -> list[tuple[str, bool]]:
    """Return (token, use_webcam) pairs. .env token gets webcam, tokens.txt do not."""
    accounts: list[tuple[str, bool]] = []
    seen: set[str] = set()

    if ENV_TOKEN:
        seen.add(ENV_TOKEN)
        accounts.append((ENV_TOKEN, True))

    for token in load_file_tokens():
        if token not in seen:
            seen.add(token)
            accounts.append((token, False))

    return accounts

def start_keep_alive() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, _format: str, *_args) -> None:
            pass

    server = HTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"🌐 Keep-alive server listening on {host}:{port}")

load_env()
start_keep_alive()

ENV_TOKEN = (os.environ.get("token") or os.environ.get("TOKEN") or "").strip()
ACCOUNTS = load_accounts()
VOICE_CHANNEL_ID = os.environ.get("VOICE_CHANNEL_ID")

if not ACCOUNTS:
    print(
        "Missing tokens. Set TOKEN in env (cam account), plus TOKENS or tokens.txt for others."
    )
    sys.exit(1)

if not VOICE_CHANNEL_ID:
    print("Missing VOICE_CHANNEL_ID. Set it in .env")
    sys.exit(1)

VOICE_CHANNEL_ID = int(VOICE_CHANNEL_ID)

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.messages = True

class VoiceSelfBot(discord.Client):
    def __init__(self, slot: int, *, use_webcam: bool) -> None:
        super().__init__(intents=intents)
        self.slot = slot
        self.use_webcam = use_webcam
        self.joining = False
        self.rejoin_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None

    def label(self) -> str:
        user = self.user
        if user:
            return str(user)
        return f"account #{self.slot + 1}"

    async def get_voice_channel(self) -> discord.VoiceChannel | discord.StageChannel:
        channel = self.get_channel(VOICE_CHANNEL_ID)
        if channel is None:
            channel = await self.fetch_channel(VOICE_CHANNEL_ID)

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise ValueError(
                f"VOICE_CHANNEL_ID must be a guild voice or stage channel (got {type(channel).__name__})"
            )

        return channel

    async def set_voice_flags(
        self,
        guild: discord.Guild,
        channel_id: int,
        *,
        enable_video: bool | None = None,
    ) -> None:
        ws = guild._state._get_websocket(guild.id)
        self_video = self.use_webcam if enable_video is None else enable_video
        await ws.send_as_json(
            {
                "op": 4,
                "d": {
                    "guild_id": guild.id,
                    "channel_id": channel_id,
                    "self_mute": False if self.use_webcam else True,
                    "self_deaf": False,
                    "self_video": self_video,
                },
            }
        )

    async def refresh_voice_state(self, guild: discord.Guild, channel_id: int) -> None:
        if self.use_webcam:
            await self.set_voice_flags(guild, channel_id, enable_video=True)
            return

        await self.set_voice_flags(guild, channel_id)

    async def apply_voice_state(self, guild: discord.Guild, channel_id: int) -> None:
        if self.use_webcam:
            await self.set_voice_flags(guild, channel_id, enable_video=False)
            await asyncio.sleep(1)
            await self.set_voice_flags(guild, channel_id, enable_video=True)
            return

        await self.set_voice_flags(guild, channel_id)

    def needs_voice_refresh(self, voice: discord.VoiceState) -> bool:
        if self.use_webcam:
            return not voice.self_video or voice.self_mute
        return voice.self_video or not voice.self_mute

    def is_in_target_channel(self, member: discord.Member | None) -> bool:
        return (
            member is not None
            and member.voice is not None
            and member.voice.channel is not None
            and member.voice.channel.id == VOICE_CHANNEL_ID
        )

    async def join_voice(self) -> None:
        if self.joining:
            return
        self.joining = True

        try:
            channel = await self.get_voice_channel()
            guild = channel.guild
            member = guild.get_member(self.user.id)

            if self.is_in_target_channel(member):
                if self.needs_voice_refresh(member.voice):
                    await self.apply_voice_state(guild, channel.id)
                    if self.use_webcam:
                        print(f"📷 [{self.label()}] Enabled fake webcam in {channel.name}")
                    else:
                        print(f"🔇 [{self.label()}] Re-applied muted join in {channel.name}")
                return

            await self.apply_voice_state(guild, channel.id)
            if self.use_webcam:
                print(f"✅ [{self.label()}] Joined {channel.name} with fake webcam enabled")
            else:
                print(f"✅ [{self.label()}] Joined {channel.name} muted")
        except Exception as err:
            print(f"❌ [{self.label()}] Failed to join voice: {err}")
            self.schedule_rejoin()
        finally:
            self.joining = False

    def schedule_rejoin(self) -> None:
        if self.rejoin_task and not self.rejoin_task.done():
            return

        async def _rejoin() -> None:
            await asyncio.sleep(REJOIN_SECONDS)
            await self.join_voice()

        self.rejoin_task = asyncio.create_task(_rejoin())

    async def voice_watch(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                channel = await self.get_voice_channel()
            except (ValueError, discord.HTTPException):
                await asyncio.sleep(30)
                continue

            guild = channel.guild
            member = guild.get_member(self.user.id)

            if not self.is_in_target_channel(member):
                await self.join_voice()
            elif self.needs_voice_refresh(member.voice):
                await self.refresh_voice_state(guild, channel.id)

            await asyncio.sleep(15 if self.use_webcam else 30)

    async def on_ready(self) -> None:
        mode = "CAM" if self.use_webcam else "MUTED"
        print(f"✅ [{self.label()}] Logged in ({mode})")
        await self.join_voice()
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self.voice_watch())

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.id != self.user.id:
            return

        if self.is_in_target_channel(member):
            if self.needs_voice_refresh(after):
                await self.refresh_voice_state(after.channel.guild, after.channel.id)
                if self.use_webcam:
                    print(f"📷 [{self.label()}] Re-enabled fake webcam")
                else:
                    print(f"🔇 [{self.label()}] Re-applied mute")
            return

        if before.channel and before.channel.id == VOICE_CHANNEL_ID and after.channel != before.channel:
            print(f"🔄 [{self.label()}] Disconnected from voice; rejoining")
            self.schedule_rejoin()

async def start_account(token: str, slot: int) -> None:
    use_webcam = token == ENV_TOKEN
    client = VoiceSelfBot(slot, use_webcam=use_webcam)
    try:
        await client.start(token, bot=False)
    except discord.LoginFailure:
        print(
            f"❌ Token #{slot + 1} rejected.\n"
            "Make sure you're using a valid user token (not a bot token)."
        )

async def run_all() -> None:
    await asyncio.gather(
        *(start_account(token, slot) for slot, (token, _) in enumerate(ACCOUNTS))
    )

def main() -> None:
    print("=" * 60)
    print("🎤 Discord Voice Selfbot")
    print(f"👥 Accounts: {len(ACCOUNTS)} ({sum(1 for _, cam in ACCOUNTS if cam)} with cam)")
    print(f"🎤 Voice Channel ID: {VOICE_CHANNEL_ID}")
    print("⚠️  WARNING: Self-bots violate Discord ToS")
    print("=" * 60)

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        print("Shutting down...")

if __name__ == "__main__":
    main()
