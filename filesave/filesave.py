import io
from typing import Union

import aiofiles
import discord
from aiofiles import os
from discord.ext import commands

from bot import ModmailBot, checks


# Todo:
# Maybe send multiple attachments per messsage...


class ConfirmView(discord.ui.View):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.value = None

	@discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
	async def y(self, interaction: discord.Interaction, button):
		self.value = True
		await interaction.response.edit_message(view=None)
		await interaction.message.add_reaction("✅")
		self.stop()

	@discord.ui.button(label="No", style=discord.ButtonStyle.red)
	async def n(self, interaction: discord.Interaction, button: discord.Button):
		self.value = False
		await interaction.response.edit_message(view=None)
		await interaction.message.add_reaction("❌")
		self.stop()


async def confirmation(ctx: discord.ext.commands.Context, message: str):
	"""Send a confirmation message with Y/N buttons.
	:param ctx: Command context.
	:param message: The text sent along with the message.
	:return: A value that can be used to determine what button was clicked.
	"""
	view = ConfirmView()
	await ctx.send(message, view=view)
	await view.wait()
	return view.value


class FileSave(commands.Cog):
	"""Lets you save files sent in a thread."""

	def __init__(self, bot):
		self.bot: ModmailBot = bot
		self.attachments_channel = self.bot.log_channel
		self.threads = None
		self.db: motor.core.AgnosticCollection = bot.api.get_plugin_partition(self)

	async def cog_unload(self):
		if await os.path.exists("./temp/filesave/"):
			await os.rmdir("./temp/filesave/")

	async def cog_load(self):
		if not await os.path.exists("./temp/filesave/"):
			await os.mkdir("./temp/filesave/")
		if config := await self.db.find_one({"_id": "filesave"}):
			try:
				self.attachments_channel = self.bot.get_channel(int(config["attachments_channel"]))
			except TypeError:
				pass
			self.threads = config["threads"]
		else:
			await self.db.insert_one({"_id": "filesave", "attachments_channel": None, "threads": []})

	async def savefile(self, message: discord.Message, channel: str):
		for att in message.attachments:
			async with self.bot.session.get(att.url) as resp:
				file = await resp.read()
				if "image" in att.content_type:
					with io.BytesIO(file) as img:
						msg = await self.attachments_channel.send(file=discord.File(img, att.filename))
				else:
					path = f"./temp/filesave/{att.filename}"
					async with aiofiles.open(path, mode="wb") as f:
						await f.write(file)
						msg = await self.attachments_channel.send(file=discord.File(path))
					await os.remove(path)
			await self.bot.db["logs"].update_one(
				{"channel_id": channel},
				{"$set": {"messages.$[].attachments.$[x].url": msg.attachments[0].url}},
				array_filters=[{"x.url": att.url}],
			)

	@commands.Cog.listener()
	async def on_message(self, message: discord.Message):
		if message.channel.id in self.threads and message.author.id != self.bot.user.id and message.attachments:
			await self.savefile(message, str(message.channel.id))

	@commands.Cog.listener()
	async def on_thread_ready(self, thread, creator, category, initial_message):
		self.db.update_one({"_id": "filesave"}, {"$push": {"threads": thread.channel.id}}, upsert=True)
		self.threads.append(thread.channel.id)

	@commands.Cog.listener()
	async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
		self.db.update_one({"_id": "filesave"}, {"$pull": {"threads": thread.channel.id}})
		self.threads.remove(thread.channel.id)

	@commands.group(name="filesave", aliases=["fs"], brief="FileSave commands.")
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def filesave(self, ctx):
		"""`FileSave` aims to help moderators who need to perserve files they send in threads.\n
		Whenever a mod replies to a thread and their reply has attachments, they are sent to another channel by the bot.\n
		It works out of the box; by default files are sent to the **logging channel**, but the channel can be customized.\n
		Archived files will also have their url updated in the database logs.
		"""

	@filesave.command(brief="Set the file archive channel.")
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def setchannel(self, ctx, channel_id):
		"""Set the file archive channel. It is the log channel by default."""
		try:
			channel_id = int(channel_id)
		except ValueError:
			await self.bot.add_reaction(ctx.message, "❌")
		else:
			self.attachments_channel = self.bot.get_channel(channel_id)
			await self.db.find_one_and_update({"_id": "filesave"}, {"$set": {"channel": channel.id}})
			await self.bot.add_reaction(ctx.message, "✅")

	class ArchiveChannelFlags(commands.FlagConverter, case_insensitive=True, delimiter=" ", prefix="-"):
		limit: Union[int, None] = commands.flag(name="limit", aliases=["lim"], description="Only this amount of messages")
		oldest: Union[bool, None] = commands.flag(name="oldest", description="Whether to start from the oldest messages first")
		before: Union[int, None] = commands.flag(name="before", description="After this message")
		after: Union[int, None] = commands.flag(name="after", description="Before this message")

	@filesave.command(brief="Save all attachments in a channel.")
	@checks.thread_only()
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def archivethread(self, ctx: discord.ext.commands.Context, *, flags: ArchiveChannelFlags = None):
		"""Itrates a thread's message history and archives files sent.
		### Flags
		Syntax: `-flagname argument`
		- `limit`/`lim` - Only this amount of messages. Only accounts for ones with attachments.
		- `oldest` - Whether to start from the oldest messages first.
		- `before` - Only before this date.
		- `after` - Only before this date. `oldest` is *`True`* if this is used.
		"""
		if flags and flags.limit:
			counter = 0
		else:
			counter = None
		channel = str(ctx.channel.id)
		if not flags:
			if not await confirmation(
					ctx, "This will post **every** attachment in this channel in your designated archive channel. Are you sure?"
			):
				return
		async for msg in ctx.channel.history(
			oldest_first=flags.oldest if flags and flags.oldest else None,
			after=await ctx.fetch_message(flags.after) if flags and flags.after else None,
			before=await ctx.fetch_message(flags.before) if flags and flags.after else None,
		):
			if counter is not None and counter == flags.limit:
				return await self.bot.add_reaction(ctx.message, "✅")
			if msg.attachments:
				if counter is not None:
					counter += 1
				await self.savefile(msg, channel)
		await self.bot.add_reaction(ctx.message, "✅")


async def setup(bot: ModmailBot):
	await bot.add_cog(FileSave(bot))
