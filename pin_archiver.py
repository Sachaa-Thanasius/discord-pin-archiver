from __future__ import annotations

import argparse
import asyncio
import datetime
import enum
import getpass
import logging
import os
from collections import deque
from pathlib import Path
from typing import NamedTuple, Self

import apsw
import apsw.bestpractice
import discord
import keyring
import platformdirs

try:
    import uvloop  # type: ignore
except ModuleNotFoundError:
    uvloop = None

# Set up logging.
discord.utils.setup_logging()
apsw.bestpractice.apply(apsw.bestpractice.recommended)  # type: ignore # SQLite WAL mode, logging, and other things.
log = logging.getLogger(__name__)

platformdir_info = platformdirs.PlatformDirs("discord-pin-archiver", "Sachaa-Thanasius", roaming=False)


INITIALIZATION_STATEMENT = """
CREATE TABLE IF NOT EXISTS pin_archive_settings (
    guild_id    INTEGER     PRIMARY KEY,
    channel_id  INTEGER     NOT NULL,
    pin_mode    INTEGER     NOT NULL
) STRICT, WITHOUT ROWID;
"""

SELECT_BY_GUILD_STATEMENT = """
SELECT * FROM pin_archive_settings WHERE guild_id = ?;
"""

UPSERT_ARCHIVE_STATEMENT = """
INSERT INTO pin_archive_settings (guild_id, channel_id, pin_mode)
VALUES (?, ?, ?)
ON CONFLICT (guild_id) DO UPDATE SET
    channel_id = EXCLUDED.channel_id,
    pin_mode = EXCLUDED.pin_mode
RETURNING *;
"""

UPDATE_MODE_STATEMENT = """
UPDATE pin_archive_settings SET pin_mode = ? WHERE guild_id = ? RETURNING *;
"""

UPDATE_CHANNEL_STATEMENT = """
UPDATE pin_archive_settings SET channel_id = ? WHERE guild_id = ? RETURNING *;
"""

UPDATE_CHANNEL_AND_MODE_STATEMENT = """
UPDATE pin_archive_settings SET channel_id = ? and pin_mode = ? WHERE guild_id = ? RETURNING *;
"""

DROP_ARCHIVE_STATEMENT = """
DELETE FROM pin_archive_settings WHERE guild_id = ?;
"""


class PinMode(enum.Enum):
    oldest = 1
    newest = 2


class PinArchiveLocation(NamedTuple):
    guild_id: int
    channel_id: int
    pin_mode: PinMode

    @classmethod
    def from_row(cls, row: tuple[int, int, int]) -> Self:
        return cls(row[0], row[1], PinMode(row[2]))

    def to_row(self) -> tuple[int, int, int]:
        return (self.guild_id, self.channel_id, self.pin_mode.value)

    def embed(self) -> discord.Embed:
        return (
            discord.Embed(title="Current Pin Archive Settings")
            .add_field(name="Channel", value=f"<#{self.channel_id}>")
            .add_field(name="Mode", value=f"The `{self.pin_mode.name}` pins will be priorites for archival.")
        )


def _setup_db(conn: apsw.Connection) -> None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(INITIALIZATION_STATEMENT)


def _query(conn: apsw.Connection, query_str: str, params: apsw.Bindings | None = None) -> list[PinArchiveLocation]:
    with conn:
        cursor = conn.cursor()
        return [PinArchiveLocation.from_row(row) for row in cursor.execute(query_str, params)]


def _upsert(conn: apsw.Connection, location: PinArchiveLocation) -> list[PinArchiveLocation]:
    with conn:
        cursor = conn.cursor()
        return [PinArchiveLocation.from_row(row) for row in cursor.execute(UPSERT_ARCHIVE_STATEMENT, location.to_row())]


def _drop(conn: apsw.Connection, guild_id: int) -> None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(DROP_ARCHIVE_STATEMENT, (guild_id,))


def resolve_path_with_links(path: Path, folder: bool = False) -> Path:
    """Resolve a path strictly with more secure default permissions, creating the path if necessary.

    Python only resolves with strict=True if the path exists.

    Source: https://github.com/mikeshardmind/discord-rolebot/blob/4374149bc75d5a0768d219101b4dc7bff3b9e38e/rolebot.py#L350
    """

    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        path = resolve_path_with_links(path.parent, folder=True) / path.name
        if folder:
            path.mkdir(mode=0o700)  # python's default is world read/write/traversable... (0o777)
        else:
            path.touch(mode=0o600)  # python's default is world read/writable... (0o666)
        return path.resolve(strict=True)


def create_pin_embed(message: discord.Message) -> discord.Embed:
    """Turn the contents of a message into an embed."""

    embed = (
        discord.Embed(colour=discord.Colour.dark_purple(), description=message.content, timestamp=message.created_at)
        .set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
        )
        .add_field(name="Source", value=f"[Jump!]({message.jump_url})")
        .set_footer(text=f"{message.author.id} • In #{message.channel}")
    )

    if message.attachments:
        embed.set_image(url=message.attachments[0].url)
    return embed


pin_group = discord.app_commands.Group(
    name="pin",
    description="Commands for controlling your pin archive.",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@pin_group.command(name="setup")
async def pin_setup(
    itx: discord.Interaction[PinArchiverBot], archive_channel: discord.TextChannel, mode: PinMode = PinMode.oldest
) -> None:
    """Set up your pin archive settings. If you've set them up previously, this will update those settings.

    Attributes
    ----------
    itx: :class:`discord.Interaction`
        The invocation interaction.
    archive_channel: :class:`discord.TextChannel`
        The channel where the pins will be stored.
    mode: :class:`PinMode`, default=PinMode.oldest
        Which pin gets sent to the pin archive channel whenever a new message is pinned and there are no pins left.
    """

    assert itx.guild  # Known at runtime.

    await itx.response.defer()
    location = await itx.client.upsert_archive_channel(itx.guild.id, archive_channel.id, mode)
    if location:
        await itx.followup.send(embed=location.embed())


@pin_group.command(name="update")
async def pin_update(
    itx: discord.Interaction[PinArchiverBot],
    archive_channel: discord.TextChannel | None = None,
    mode: PinMode | None = None,
) -> None:
    """Updates your pin archive settings. Every input is optional: If not given, the previously set value will be kept.

    Attributes
    ----------
    itx: :class:`discord.Interaction`
        The invocation interaction.
    archive_channel: :class:`discord.TextChannel`, optional
        The channel where the pins will be stored. Defaults to None.
    mode: :class:`PinMode`, optional
        Which pin gets sent to the pin archive channel whenever a new message is pinned and there are no pins left. Defaults to None.
    """

    assert itx.guild  # Known at runtime.

    await itx.response.defer()

    if not archive_channel and not mode:
        await itx.followup.send("No new settings put in: No changes made.")
        return

    location = await itx.client.update_archive_channel(itx.guild.id, archive_channel, mode)
    if location:
        embed = location.embed()
        embed.title = "Updated Pin Archive Settings"
        await itx.followup.send(embed=embed)


@pin_group.command(name="current")
async def pin_current(itx: discord.Interaction[PinArchiverBot]) -> None:
    """Displays the current pin archive settings for this server."""

    assert itx.guild  # Known at runtime.

    await itx.response.defer()
    location = await itx.client.get_archive_channel(itx.guild.id)
    if location:
        await itx.followup.send(embed=location.embed())
    else:
        await itx.followup.send("The pin archive has not be set up for this server.")


@pin_group.command(name="disable")
async def pin_disable(itx: discord.Interaction[PinArchiverBot]) -> None:
    """Disable the pin archive in this server. The bot will no longer actively move pins.

    Note: If you wish to enable this again, use /pin setup.
    """

    assert itx.guild  # Known at runtime.

    await itx.response.defer()
    await asyncio.to_thread(_drop, itx.client.db_connection, itx.guild.id)
    await itx.followup.send("The bot will no longer update the pin archive. To re-enable, use `/pin setup`.")


@discord.app_commands.command()
async def invite(itx: discord.Interaction[PinArchiverBot]) -> None:
    """Get a link to invite this bot to a server."""

    embed = discord.Embed(description="Click the link below to invite me to one of your servers.")
    view = discord.ui.View().add_item(discord.ui.Button(label="Invite", url=itx.client.invite_link))
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)


APP_COMMANDS = (pin_group, invite)


class PinArchiverBot(discord.AutoShardedClient):
    def __init__(self) -> None:
        super().__init__(
            intents=discord.Intents(guilds=True, members=True, guild_messages=True, message_content=True),
            activity=discord.Game(name="https://github.com/Sachaa-Thanasius/discord-pin-archiver"),
        )
        self.tree = discord.app_commands.CommandTree(self)

        # Connect to the database that will store the archive information.
        # -- Need to account for the directories and/or file not existing.
        db_path = platformdir_info.user_data_path / "pin_archiver_data.db"
        resolved_path_as_str = str(resolve_path_with_links(db_path))
        self.db_connection = apsw.Connection(resolved_path_as_str)

        self._guard_stack: deque[int] = deque()

    async def on_connect(self: Self) -> None:
        """(Re)set the client's general invite link every time it (re)connects to the Discord Gateway."""

        await self.wait_until_ready()
        data = await self.application_info()
        perms = discord.Permissions(321536)
        self.invite_link = discord.utils.oauth_url(data.id, permissions=perms)

    async def setup_hook(self) -> None:
        # Initialize the database and start the loop.
        await asyncio.to_thread(_setup_db, self.db_connection)

        # Add the app commands to the tree.
        for cmd in APP_COMMANDS:
            self.tree.add_command(cmd)

    async def on_guild_channel_pins_update(
        self, channel: discord.abc.GuildChannel | discord.Thread, last_pin: datetime.datetime | None = None
    ) -> None:
        """Listen to guild-level pin events and move pins as necessary."""

        location = await self.get_archive_channel(channel.guild.id)
        if not location:
            return

        try:
            # Known to exist since this event was triggered. Also  is guarded.
            current_pins: list[discord.Message] = await channel.pins()  # type: ignore
        except (AttributeError, discord.HTTPException):
            log.exception(
                "Couldn't access the channel's pins: guild_id=%s, channel_id=%s, channel_name=%s",
                channel.guild.id,
                channel.id,
                channel.mention,
            )
        else:
            assert isinstance(current_pins, list)
            if len(current_pins) < 49:
                return

            archive_channel = channel.guild.get_channel(location.channel_id)
            assert isinstance(archive_channel, discord.TextChannel)

            try:
                pin = current_pins[-1] if (location.pin_mode is PinMode.oldest) else current_pins[0]
                try:
                    old_pin = self._guard_stack.pop()
                except IndexError:
                    self._guard_stack.append(pin.id)
                else:
                    if old_pin == pin.id:
                        return

                await pin.unpin(reason="Moving pin to archive channel.")
                embed = create_pin_embed(pin)
                await archive_channel.send(embed=embed)
            except (IndexError, discord.HTTPException) as err:
                log.exception("", exc_info=err)

        log.info("on_guild_channel_pins_update(): %s, %s, %s", channel.guild, channel, last_pin)

    async def upsert_archive_channel(
        self, guild_id: int, channel_id: int, pin_mode: PinMode
    ) -> PinArchiveLocation | None:
        new_location = PinArchiveLocation(guild_id, channel_id, pin_mode)
        locations = await asyncio.to_thread(_upsert, self.db_connection, new_location)
        return locations[0] if locations else None

    async def get_archive_channel(self, guild_id: int) -> PinArchiveLocation | None:
        locations = await asyncio.to_thread(_query, self.db_connection, SELECT_BY_GUILD_STATEMENT, (guild_id,))
        return locations[0] if locations else None

    async def update_archive_channel(
        self, guild_id: int, channel: discord.TextChannel | None, pin_mode: PinMode | None
    ) -> PinArchiveLocation | None:
        if channel and pin_mode:
            locations = await asyncio.to_thread(
                _query, self.db_connection, UPDATE_CHANNEL_AND_MODE_STATEMENT, (channel.id, pin_mode.value, guild_id)
            )
        elif channel:
            locations = await asyncio.to_thread(
                _query, self.db_connection, UPDATE_CHANNEL_STATEMENT, (channel.id, guild_id)
            )
        elif pin_mode:
            locations = await asyncio.to_thread(
                _query, self.db_connection, UPDATE_MODE_STATEMENT, (pin_mode.value, guild_id)
            )
        else:
            return None

        return locations[0] if locations else None


def _get_keyring_creds() -> str | None:
    user = getpass.getuser()
    return keyring.get_password("discord-pin-archiver", user)


def _set_keyring_creds(token: str, /) -> None:
    user = getpass.getuser()
    keyring.set_password("discord-pin-archiver", user, token)


def _get_token() -> str:
    token = os.getenv("PIN_ARCHIVER_TOKEN") or _get_keyring_creds()
    if not token:
        msg = (
            "NO TOKEN? (Use Environment `PIN_ARCHIVER_TOKEN` or launch with `--setup` to go through interactive setup)"
        )
        raise RuntimeError(msg) from None
    return token


def run_bot() -> None:
    async def bot_runner() -> None:
        async with PinArchiverBot() as client:
            await client.start(token, reconnect=True)

    token = _get_token()
    loop = uvloop.new_event_loop if (uvloop is not None) else None  # type: ignore
    with asyncio.Runner(loop_factory=loop) as runner:  # type: ignore
        runner.run(bot_runner())


def run_setup() -> None:
    prompt = (
        "Paste the discord token you'd like to use for this bot here (won't be visible) then press enter. "
        "This will be stored in the system keyring for later use >"
    )
    token = getpass.getpass(prompt)
    if not token:
        msg = "Not storing empty token"
        raise RuntimeError(msg)
    _set_keyring_creds(token)


def main() -> None:
    parser = argparse.ArgumentParser(description="A minimal configuration discord bot for automatic pin archiving.")
    excl = parser.add_mutually_exclusive_group()
    excl.add_argument("--setup", action="store_true", default=False, help="Run interactive setup.", dest="isetup")
    excl.add_argument(
        "--set-token-to",
        default=None,
        dest="token",
        help="Provide a token directly to be stored in the system keyring.",
    )
    args = parser.parse_args()
    if args.isetup:
        run_setup()
    elif args.token:
        _set_keyring_creds(args.token)
    else:
        run_bot()


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())