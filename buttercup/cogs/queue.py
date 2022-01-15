import logging
from datetime import datetime, timedelta
from typing import Dict

import dateutil.parser
import pandas as pd
import pytz
from blossom_wrapper import BlossomAPI
from discord import Embed
from discord.ext import tasks
from discord.ext.commands import Cog
from discord_slash import SlashContext, cog_ext
from discord_slash.model import SlashMessage

from buttercup.bot import ButtercupBot
from buttercup.cogs.find import COMPLETED_COLOR, IN_PROGRESS_COLOR, UNCLAIMED_COLOR
from buttercup.cogs.helpers import (
    BlossomException,
    get_discord_time_str,
    get_submission_source,
)
from buttercup.strings import translation

logger = logging.Logger("queue")

i18n = translation()


def extract_user_id(user_url: str) -> str:
    """Extract the ID from a Blossom user URL."""
    return user_url.split("/")[-2]


def fix_submission_source(submission: Dict) -> Dict:
    """Fix the source of the submission to be the subreddit."""
    return {
        **submission,
        "source": get_submission_source(submission),
    }


def get_unclaimed_list(sources: pd.Series) -> str:
    """Get a list of the posts grouped by sources."""
    items = [
        i18n["queue"]["unclaimed_list_entry"].format(count=count, source=source)
        for source, count in sources.head(5).iteritems()
    ]
    result = "\n".join(items)

    if len(sources) > 5:
        rest = sources[5:]
        source_count = len(rest)
        post_count = rest.sum()
        result += "\n" + i18n["queue"]["unclaimed_list_others"].format(
            post_count=post_count, source_count=source_count
        )

    return result


def get_claimed_item(submission: pd.Series, user_cache: Dict) -> str:
    """Get the formatted submission item."""
    source = submission["source"]
    time_str = submission["claim_time"]
    url = submission["tor_url"]
    author_url = submission["claimed_by"]

    time = get_discord_time_str(dateutil.parser.parse(time_str), style="R")
    author_id = extract_user_id(author_url)
    author = user_cache.get(author_id, {"username": author_id})

    return i18n["queue"]["claimed_list_entry"].format(
        author="u/" + author["username"], source=source, url=url, time=time,
    )


def get_claimed_list(claimed: pd.DataFrame, user_cache: Dict) -> str:
    """Get a list of claimed submissions."""
    items = [
        get_claimed_item(submission, user_cache)
        for idx, submission in claimed.head(5).iterrows()
    ]
    result = "\n".join(items)

    if len(claimed) > 5:
        rest = claimed[5:]
        result += "\n" + i18n["queue"]["claimed_list_others"].format(
            other_count=len(rest)
        )

    return result


class Queue(Cog):
    def __init__(self, bot: ButtercupBot, blossom_api: BlossomAPI) -> None:
        """Initialize the Queue cog."""
        self.bot = bot
        self.blossom_api = blossom_api

        self.last_update = datetime.now()
        self.unclaimed = None
        self.claimed = None
        self.user_cache = {}
        self.messages = []

        logger.info("Starting queue update cycle...")
        self.update_cycle.start()

    @tasks.loop(minutes=1)
    async def update_cycle(self) -> None:
        """Keep everything up-to-date."""
        await self.update_queue()
        await self.update_messages()

    async def update_queue(self) -> None:
        """Update the cached queue items."""
        self.unclaimed = await self.get_unclaimed_submissions()
        self.claimed = await self.get_claimed_submissions()
        self.update_user_cache()

        self.last_update = datetime.now()

    async def update_messages(self) -> None:
        """Update all messages with the latest queue stats."""
        for msg in self.messages:
            await self.update_message(msg)

    async def get_unclaimed_submissions(self) -> pd.DataFrame:
        """Get the submissions that are currently unclaimed in the queue."""
        # Posts older than 18 hours are archived
        queue_start = datetime.now(tz=pytz.utc) - timedelta(hours=18)
        results = []
        size = 500
        page = 1

        # Fetch all unclaimed posts from the queue
        while True:
            queue_response = self.blossom_api.get(
                "submission/",
                params={
                    "page_size": size,
                    "page": page,
                    "completed_by__isnull": True,
                    "claimed_by__isnull": True,
                    "archived": False,
                    "create_time__gte": queue_start.isoformat(),
                },
            )
            if not queue_response.ok:
                raise BlossomException(queue_response)

            data = queue_response.json()["results"]
            data = [fix_submission_source(entry) for entry in data]
            results += data
            page += 1

            if len(data) < size:
                break

        data_frame = pd.DataFrame.from_records(data=results, index="id")
        return data_frame

    async def get_claimed_submissions(self) -> pd.DataFrame:
        """Get the submissions that are currently in progress."""
        # Only consider recent posts that may still be worked on
        queue_start = datetime.now(tz=pytz.utc) - timedelta(hours=48)
        results = []
        size = 500
        page = 1

        # Fetch all claimed posts from the queue
        while True:
            queue_response = self.blossom_api.get(
                "submission/",
                params={
                    "page_size": size,
                    "page": page,
                    "completed_by__isnull": True,
                    "claimed_by__isnull": False,
                    "archived": False,
                    "create_time__gte": queue_start.isoformat(),
                    "ordering": "-claim_time",
                },
            )
            if not queue_response.ok:
                raise BlossomException(queue_response)

            data = queue_response.json()["results"]
            data = [fix_submission_source(entry) for entry in data]
            results += data
            page += 1

            if len(data) < size:
                break

        data_frame = pd.DataFrame.from_records(data=results, index="id")
        return data_frame

    def update_user_cache(self) -> None:
        """Fetch the users from their IDs."""
        user_cache = {}

        for idx, submission in self.claimed.head(5).iterrows():
            user_id = extract_user_id(submission["claimed_by"])

            if user := self.user_cache.get(user_id):
                # Take the user from the old cache, if available
                user_cache[user_id] = user

            user_response = self.blossom_api.get("volunteer", params={"id": user_id})
            if not user_response.ok:
                raise BlossomException(user_response)
            user = user_response.json()["results"][0]
            user_cache[user_id] = user

        self.user_cache = user_cache

    def add_message(self, msg: SlashMessage) -> None:
        """Add a new message to update with the current queue stats.

        This enforces a maximum amount of messages that should
        be kept updated, to improve performance.
        """
        limit = 5
        self.messages = self.messages[-(limit - 1) :] + [msg]

    @cog_ext.cog_slash(
        name="queue", description="Display the current status of the queue.",
    )
    async def queue(self, ctx: SlashContext) -> None:
        """Display the current status of the queue."""
        # Send a first message to show that the bot is responsive.
        # We will edit this message later with the actual content.
        msg = await ctx.send(i18n["queue"]["getting_queue"])
        # Update the message with the last known stats
        await self.update_message(msg)
        # Keep the message updated in the future
        self.add_message(msg)

    async def update_message(self, msg: SlashMessage) -> None:
        """Update the given message with the latest queue stats."""
        unclaimed = self.unclaimed
        unclaimed_count = len(unclaimed.index)

        claimed = self.claimed
        claimed_count = len(claimed.index)

        sources = (
            unclaimed.reset_index()
            .groupby(["source"])["id"]
            .count()
            .sort_values(ascending=False)
        )
        unclaimed_list = get_unclaimed_list(sources)

        unclaimed_message = (
            i18n["queue"]["unclaimed_message_cleared"]
            if unclaimed_count == 0
            else i18n["queue"]["unclaimed_message"].format(
                unclaimed_count=unclaimed_count, unclaimed_list=unclaimed_list,
            )
        )

        claimed_list = get_claimed_list(self.claimed, self.user_cache)

        claimed_message = (
            i18n["queue"]["claimed_message_cleared"]
            if claimed_count == 0
            else i18n["queue"]["claimed_message"].format(
                claimed_count=claimed_count, claimed_list=claimed_list
            )
        )

        color = (
            COMPLETED_COLOR
            if unclaimed_count == 0
            else IN_PROGRESS_COLOR
            if claimed_count > 0
            else UNCLAIMED_COLOR
        )

        embed = Embed(
            title=i18n["queue"]["embed_title"],
            description=i18n["queue"]["embed_description"].format(
                unclaimed_message=unclaimed_message, claimed_message=claimed_message,
            ),
            color=color,
        )

        await msg.edit(
            content=i18n["queue"]["embed_message"].format(
                last_updated=get_discord_time_str(date_time=self.last_update, style="R")
            ),
            embed=embed,
        )


def setup(bot: ButtercupBot) -> None:
    """Set up the Queue cog."""
    cog_config = bot.config["Blossom"]
    email = cog_config.get("email")
    password = cog_config.get("password")
    api_key = cog_config.get("api_key")
    blossom_api = BlossomAPI(email=email, password=password, api_key=api_key)
    bot.add_cog(Queue(bot=bot, blossom_api=blossom_api))


def teardown(bot: ButtercupBot) -> None:
    """Unload the Queue cog."""
    bot.remove_cog("Queue")
