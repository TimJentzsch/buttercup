from discord.ext.commands import Cog
from discord import Embed, Color
from discord_slash import SlashContext, cog_ext
from discord_slash.utils.manage_commands import create_option
from urllib.parse import urlparse

from typing import Optional, Dict, Any

from buttercup.bot import ButtercupBot
from blossom_wrapper import BlossomAPI, BlossomStatus

from buttercup.objects.submission import Submission


class Lookup(Cog):
    def __init__(self, bot: ButtercupBot, blossom: BlossomAPI) -> None:
        """Initialize the Lookup cog."""
        self.bot = bot
        self.blossom = blossom

    @staticmethod
    def _parse_reddit_url(reddit_url_str: str) -> Optional[str]:
        """
        Tries to parse and normalize the given Reddit URL.

        :returns: The normalized Reddit URL or None if the parsing failed.
        """
        parse_result = urlparse(reddit_url_str)

        if "reddit" not in parse_result.netloc:
            return None

        # On Blossom, all URLs end with a slash
        path = parse_result.path
        if not path.endswith("/"):
            path += "/"

        return path

    @cog_ext.cog_slash(
        name="lookup",
        description="Find a post given a Reddit URL.",
        options=[
            create_option(
                name="reddit_url",
                description="A Reddit URL, either to the submission on ToR, the partner sub or the transcription.",
                option_type=3,
                required=True,
            )
        ],
    )
    async def _lookup(self, ctx: SlashContext, reddit_url: str) -> None:
        """Look up the post with the given URL."""

        path = Lookup._parse_reddit_url(reddit_url)
        normalized_url = f"https://reddit.com{path}"

        # Send a first message to show that the bot is responsive.
        # We will edit this message later with the actual content.
        msg = await ctx.send(f"Looking for post <{normalized_url}>...")

        if normalized_url is None:
            await msg.edit(content=f"I don't recognize <{reddit_url}> as valid Reddit URL. Please provide a link to "
                           "either a post on a r/TranscribersOfReddit, on a partner sub or to a transcription.")
            return

        if "/r/TranscribersOfReddit" in path:
            # It's a link to the ToR submission
            response = self.blossom.get_submission(tor_url=normalized_url)
        elif len(path.split("/")) >= 8:
            # It's a comment on a partner sub, i.e. a transcription
            # This means that the path is longer, because of the added comment ID
            tr_response = self.blossom.get_transcription(url=normalized_url)
            tr_data = tr_response.data

            if tr_response.status != BlossomStatus.ok or tr_data is None or len(tr_data) == 0:
                response = None
            else:
                transcription = tr_data[0]
                # We don't have direct access to the submission ID, so we need to extract it from the submission URL
                submission_url = transcription["submission"]
                submission_id = submission_url.split("/")[-2]
                response = self.blossom.get_submission(id=submission_id)
        else:
            # It's a link to the submission on a partner sub
            response = self.blossom.get_submission(url=normalized_url)

        if response is None or response.status != BlossomStatus.ok or response.data is None or len(response.data) == 0:
            await msg.edit(content=f"Sorry, I couldn't find a post with the URL <{normalized_url}>.")
            return

        submission = Submission(response.data[0])
        await msg.edit(content="I found your post!", embed=submission.to_embed())


def setup(bot: ButtercupBot) -> None:
    """Set up the Lookup cog."""
    cog_config = bot.config["Blossom"]
    email = cog_config.get("email")
    password = cog_config.get("password")
    api_key = cog_config.get("api_key")
    blossom = BlossomAPI(email=email, password=password, api_key=api_key)
    bot.add_cog(Lookup(bot, blossom))


def teardown(bot: ButtercupBot) -> None:
    """Unload the Lookup cog."""
    bot.remove_cog("Lookup")
