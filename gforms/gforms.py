import datetime
import json
import os
import re
from typing import Union, Tuple

import aiofiles
import aiogoogle
import aiogoogle.auth
import discord
import jsonschema
import motor.core
from discord.ext import commands, tasks

from bot import ModmailBot, checks
from core import paginator as pages, models

# TODO:
# - Add "?gforms form" command basically only for getting question Item IDs
# - Add the option to exclude posting specific answers
# - Toggle whether questions with no answers are shown or not

KEY_FILE = "service_account_key.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]

key_schema = {
	"type": "object",
	"properties": {
		"type": {"type": "string"},
		"project_id": {"type": "string"},
		"private_key_id": {"type": "string"},
		"private_key": {"type": "string"},
		"client_email": {"type": "string"},
		"client_id": {"type": "string"},
		"auth_uri": {"type": "string"},
		"token_uri": {"type": "string"},
		"auth_provider_x509_cert_url": {"type": "string"},
		"client_x509_cert_url": {"type": "string"},
		"universe_domain": {"type": "string"},
	},
	"required": [
		"type",
		"project_id",
		"private_key_id",
		"client_email",
		"client_id",
		"auth_uri",
		"token_uri",
		"auth_provider_x509_cert_url",
		"client_x509_cert_url",
		"universe_domain",
	],
}

logger: models.ModmailLogger = models.getLogger(__name__)


class Embed(discord.Embed):
	def __init__(self, color=discord.Color.from_rgb(114, 72, 185), *args, **kwargs):
		super().__init__(color=color, *args, **kwargs)


class ConfirmView(discord.ui.View):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.value = None

	@discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
	async def y(self, interaction: discord.Interaction, button):
		await interaction.message.delete()
		self.value = True
		self.stop()

	@discord.ui.button(label="No", style=discord.ButtonStyle.red)
	async def n(self, interaction: discord.Interaction, button):
		await interaction.message.delete()
		self.value = False
		self.stop()


class ServiceEmailView(discord.ui.View):
	def __init__(self, author_id, *args, **kwargs):
		self.author = author_id
		super().__init__(*args, **kwargs)

	async def interaction_check(self, interaction: discord.Interaction):
		return interaction.user.id == self.author

	@discord.ui.button(label="Show", style=discord.ButtonStyle.primary)
	async def show(self, interaction: discord.Interaction, button):
		async with aiofiles.open(KEY_FILE, mode="r") as f:
			sa_json = await f.read()
			sa_json = json.loads(sa_json)
		return await interaction.response.send_message(sa_json["client_email"], ephemeral=True)


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


async def get_time(time: str, now: datetime.datetime = None):
	"""Get a form watch time.
	:param time: The time (hour and minute) to use for the calculation.
	:param now: The current time.
	:return: (datetime, datetime)
	"""
	if not now:
		now = datetime.datetime.now(datetime.timezone.utc)
	when = datetime.datetime.strptime(f"{now.date()} {time}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
	if when < datetime.datetime.now(datetime.timezone.utc):
		when = when + datetime.timedelta(days=1)
	return when


async def is_set_up(ctx: commands.Context = None):
	if not os.path.exists(KEY_FILE):
		if ctx:
			await ctx.send("There is not currently a key file. Use `?gforms setup`.")
		return False
	else:
		return True


async def validate_channel(ctx: commands.Context, channel_id=None, check_permissions: bool = True):
	"""Validate a server channel id."""
	if channel_id:
		try:
			channel_id = int(channel_id)
		except ValueError:
			await ctx.send("Invalid channel.")
			return False
		else:
			channel = ctx.guild.get_channel_or_thread(channel_id)
	else:
		channel = ctx.channel

	if channel:
		if check_permissions:
			if channel.permissions_for(ctx.guild.me).view_channel and channel.permissions_for(ctx.guild.me).send_messages:
				return channel
			else:
				await ctx.send("I cannot view or send messages in that channel.")
				return False
		else:
			return channel

	else:
		await ctx.send("Invalid channel.")
		return False


async def send_response(
	aiogoogle,
	service: aiogoogle.resource.GoogleAPI,
	form_id: str,
	response: dict,
	destination: Union[discord.abc.GuildChannel, commands.Context],
):
	form = await aiogoogle.as_service_account(service.forms.get(formId=form_id))
	message = await GFormResponses(form, response).read()
	if isinstance(destination, commands.Context):
		await message.send(ctx=destination)
	else:
		await message.send(channel=destination)


def listsplit(num: int, li: Union[list, tuple]):
	"""Makes multiple lists out of a long list.
	:param num: The number threshold until the list gets split.
	:param li: The list to split Into multiple embeds.
	:return: list
	"""
	results = [li[x : x + num] for x in range(0, len(li), num)]
	return results


class GFormResponses:
	def __init__(self, form: dict, response: dict):
		self.form = form
		self.response = response
		self.response_submit_time = datetime.datetime.strptime(
			re.sub("\..+?(?=Z)", "", self.response["lastSubmittedTime"]), "%Y-%m-%dT%H:%M:%SZ"
		)
		self.title = self.form["info"].get("title", self.form["info"]["documentTitle"])
		self.answers = None
		self._embed = None
		self._embeds: Union[list | [list]] = []

	async def read(self):
		self._embed = Embed(title=self.title, description="", timestamp=self.response_submit_time).set_footer(
			text=f'Response ID {self.response["responseId"]}'
		)

		await self.split_embed(self.form["info"].get("description", ""))

		for form_item in self.form["items"]:
			try:
				if form_item.get("questionGroupItem"):
					question_ids = [i["questionId"] for i in form_item["questionGroupItem"]["questions"]]
				else:
					question_ids = [form_item["questionItem"]["question"]["questionId"]]
			except KeyError:
				continue
			if answers := self.response.get("answers"):
				self.answers = answers
			else:
				self._embed.description = self._embed.description + "\n\n*This response has no answers.*"
				break
			await self.build_embed(form_item, question_ids)
		if self._embed not in self._embeds:
			self._embeds.append(self._embed)
		return self

	async def build_embed(self, item, ids: list):
		"""Generate embeds from a Google Form response."""
		if self.answers:
			if any([i in self.answers for i in ids]):
				q_title = f'\n### {item.get(f"title", "*(empty)*")}\n'
				q_desc = item.get(f"description", "") + "\n"

				if group_item := item.get("questionGroupItem"):
					reg = re.compile("\s{2,}")
					q_answers = [
						f"- **{re.sub(reg, ' ', q['rowQuestion']['title'])}**\n"
						+ "\n".join([f"  - {a['value']}" for a in self.answers[q["questionId"]]["textAnswers"]["answers"]])
						for q in group_item["questions"]
						if q["questionId"] in self.answers
					]

				else:
					question_id = item["questionItem"]["question"]["questionId"]
					answers = self.answers[question_id]
					if any(k in answers for k in ("fileUploadAnswers", "textAnswers")):
						if "fileUploadAnswers" in answers:
							q_answers = [
								f'- https://drive.google.com/file/d/{a["fileId"]}/view' for a in answers["fileUploadAnswers"]["answers"]
							]
						elif scale := item["questionItem"]["question"].get("scaleQuestion"):
							counter = ""

							low = scale.get("low")
							high = scale.get("high")

							value = int(answers["textAnswers"]["answers"][0]["value"])

							if low:
								for i in range(high):
									if i == value - 1:
										counter += "𒊹"
									else:
										counter += "●"
							else:
								_high = high + 1
								for i in range(_high):
									if i == value:
										counter += "𒊹"
									else:
										counter += "●"

							low_label = scale.get("lowLabel")
							high_label = scale.get("highLabel")
							q_answers = "{} {} {} {} {}".format(
								f"*{low_label}* — " if low_label is not None else "",
								low or "0",
								counter,
								high,
								f" — *{high_label}*" if high_label is not None else "",
							)

						else:
							if "textQuestion" in item["questionItem"]["question"]:
								q_answers = f"```\n{answers['textAnswers']['answers'][0]['value']}```"
							elif "choiceQuestion" in item["questionItem"]["question"]:
								q_answers = [
									f'- {"Other: " if "isOther" in item["questionItem"]["question"]["choiceQuestion"]["options"][i] else ""}{a["value"]}'
									for i, a in enumerate(answers["textAnswers"]["answers"]) or ""
								]
							else:
								q_answers = [f'- {a["value"]}' for a in answers["textAnswers"]["answers"]]
					else:
						return

				if isinstance(q_answers, list):
					q_answers = "\n".join(q_answers)

				if len(q_title) + len(self._embed.description) <= 4096:
					self._embed.description = self._embed.description + q_title
				else:
					self._embeds.append(self._embed)
					self._embed = Embed(description=q_title, timestamp=self.response_submit_time).set_footer(
						text=f'Response ID {self.response["responseId"]}'
					)

				await self.split_embed(q_desc)

				await self.split_embed(q_answers, string_type="textQuestion")

	async def split_embed(self, string: str, string_type: str = None):
		"""Split a form item over multiple embeds."""
		length = len(string)
		if 4096 < length + len(self._embed.description):
			if 4096 < length:
				pos = 0

				while pos < length:
					overflow = length + len(self._embed.description) - 4096

					split_len = length - overflow

					if string_type == "textQuestion":
						split_len -= 7

						string = re.sub("`(`+$|^`+)", "", string, flags=re.MULTILINE)

						splitted = f"```\n{string[pos: pos + split_len]}```"

					else:
						splitted = string[pos : pos + split_len]

					self._embed.description += splitted

					pos += split_len

					if not 4096 < len(self._embed.description):
						if pos < length:
							self._embeds.append(self._embed)
							self._embed = Embed(description="", timestamp=self.response_submit_time).set_footer(
								text=f'Response ID {self.response["responseId"]}'
							)
						else:
							return
			else:
				self._embeds.append(self._embed)
				self._embed = Embed(description=string, timestamp=self.response_submit_time).set_footer(
					text=f'Response ID {self.response["responseId"]}'
				)

		else:
			self._embed.description += string

	async def send(self, ctx: commands.Context = None, channel: discord.abc.GuildChannel = None):
		"""Send a response of a Google Form to Discord."""
		if self._embeds:
			for embed in self._embeds:
				if ctx:
					await ctx.send(embed=embed)
				else:
					await channel.send(embed=embed)

		else:
			if ctx:
				await ctx.send(embed=self._embed)
			else:
				await channel.send(embed=self._embed)


class GFormsPaginator(pages.PaginatorSession):
	def __init__(self, ctx: commands.Context = None, *embeds, **options):
		super().__init__(ctx, *embeds, **options)
		self.response: GFormResponses = options.get("response", None)
		self.pages = embeds

	async def create_base(self, item) -> None:
		if len(self.pages) == 1:
			self.view = None
			self.running = False
			await self.destination.send(embed=item)
		else:
			self.view = GFormsPaginatorView(self, self.pages)
			self.running = True
			await self._create_base(item, self.view)

	async def _create_base(self, item: discord.Embed, view: discord.ui.View) -> None:
		await self.destination.send(embed=item, view=view)


class GFormsPaginatorView(discord.ui.View):
	def __init__(self, handler: GFormsPaginator = None, pages=None, ids: list = None):
		super().__init__(timeout=None)

		self.ids = ids
		self.handler: GFormsPaginator = handler
		self.pages = pages
		self.current = 0

		if self.handler:
			self.page_count = len(self.handler.pages)
		else:
			self.page_count = len(self.pages)

		if self.page_count == 2:
			self.remove_item(self.children[4])
			self.remove_item(self.children[0])
			self.counter_position = 1
		else:
			self.counter_position = 2

		self.children[self.counter_position].label = f"1/{self.page_count}"

		if self.ids:
			for pos, i in enumerate(self.children):
				i.custom_id = self.ids[pos]

	async def callback(self, interaction):
		self.children[self.counter_position].label = f"{self.current + 1}/{self.page_count}"
		self.update_disabled_status()
		await interaction.response.edit_message(embed=self.pages[self.current], view=self)

	@discord.ui.button(label="<<", disabled=True, style=discord.ButtonStyle.secondary)
	async def first_callback(self, interaction: discord.Interaction, button: discord.Button):
		self.current = self.first_page()
		await self.callback(interaction)

	@discord.ui.button(label="<", disabled=True, style=discord.ButtonStyle.primary)
	async def back_callback(self, interaction: discord.Interaction, button: discord.Button):
		self.current = self.previous_page()
		await self.callback(interaction)

	@discord.ui.button(disabled=True, label="/", style=discord.ButtonStyle.grey)
	async def label_callback(self, interaction: discord.Interaction, button: discord.Button):
		pass

	@discord.ui.button(label=">", style=discord.ButtonStyle.primary)
	async def next_callback(self, interaction: discord.Interaction, button: discord.Button):
		self.current = self.next_page()
		await self.callback(interaction)

	@discord.ui.button(label=">>", style=discord.ButtonStyle.secondary)
	async def last_callback(self, interaction: discord.Interaction, button: discord.Button):
		self.current = self.last_page()
		await self.callback(interaction)

	def update_disabled_status(self):
		if self.current == self.first_page():
			for i in self.children:
				if i.label == "<<" or i.label == "<":
					i.disabled = True
		else:
			for i in self.children:
				if i.label == "<<" or i.label == "<":
					i.disabled = False

		if self.current == self.last_page():
			for i in self.children:
				if i.label == ">>" or i.label == ">":
					i.disabled = True
		else:
			for i in self.children:
				if i.label == ">>" or i.label == ">":
					i.disabled = False

	def first_page(self):
		return 0

	def next_page(self):
		return min(self.current + 1, self.last_page())

	def previous_page(self):
		return max(self.current - 1, self.first_page())

	def last_page(self):
		return self.page_count - 1


class GForms(commands.Cog):
	"""Integrates Google Forms into Modmail."""

	def __init__(self, bot):
		self.bot: ModmailBot = bot
		self.creds = None
		self.db: motor.core.AgnosticCollection = bot.api.get_plugin_partition(self)

	@tasks.loop()
	async def form_watch(self):
		task = None

		query = self.db.find().sort([("when", 1)]).limit(1)

		async for watch in query:
			task = watch

		if task is None:
			return self.form_watch.cancel()

		await discord.utils.sleep_until(task["when"].replace(tzinfo=datetime.timezone.utc))
		since = task["since"].replace(tzinfo=datetime.timezone.utc)

		async with aiogoogle.Aiogoogle(service_account_creds=self.creds) as aiog:
			service = await aiog.discover("forms", "v1", disco_doc_ver=2)
			_form = await aiog.as_service_account(service.forms.get(formId=task["form_id"]))
			title = task["form_title"]
			if channel := self.bot.get_channel(task["channel_id"]):
				now = datetime.datetime.now(datetime.timezone.utc)

				nextpagetoken = None

				if "hours" in task:
					update = {"$set": {"since": now, "when": task["when"] + datetime.timedelta(hours=task["hours"])}}
				else:
					update = {"$set": {"since": now, "when": await get_time(task["time"])}}

				while True:
					if responses := await aiog.as_service_account(
						service.forms.responses.list(
							formId=task["form_id"],
							filter=f"timestamp >= {since.isoformat().replace('+00:00', 'Z')}",
							nextPageToken=nextpagetoken,
						)
					):
						try:
							if nextpagetoken is None:
								content = f"**{title}**: Responses since {since.strftime('%B %d at %H:%M:%S')} :arrow_heading_down:"
								if "pings" in task:
									content = f'{",".join(task["pings"])}\n{content}'
								await channel.send(content)
								if "message_id" in task:
									update["$unset"] = {"message_id": ""}
							for response in responses["responses"]:
								await send_response(aiog, service, task["form_id"], response, channel)
							if "nextPageToken" in responses:
								nextpagetoken = responses["nextPageToken"]
							else:
								break
						except discord.Forbidden:
							logger.warning(f"{channel.guild.name}: Could not send responses to {channel.name}.")
							break
					else:
						content = f"**{title}**: No responses have been submitted since {since.strftime('%B %d at %H:%M:%S')}."
						if "message_id" in task:
							await channel.get_partial_message(task["message_id"]).edit(content=content)
						else:
							msg = await channel.send(content)
							update["$set"]["message_id"] = msg.id
						break
				if "guild" not in task:
					update["$set"]["guild"] = channel.guild.id
				await self.db.update_one({"_id": task["_id"]}, update, upsert=False)
			else:
				if "guild" in task:
					logger.warning(f"{self.bot.get_guild(task['guild']).name}: A channel assigned to a watch ({task['channel_id']}) seems to no longer exist. The watch will be removed.")
				else:
					logger.warning(f"A channel assigned to a watch ({task['channel_id']}) seems to no longer exist. The watch will be removed.")
				await self.db.delete_one({"_id": task["_id"]})

	@form_watch.before_loop
	async def watch_before(self):
		await self.bot.wait_until_ready()

	async def cog_load(self):
		self.form_watch.start()
		if await is_set_up():
			logger.line()
			credentials = json.load(open(KEY_FILE))
			del credentials["universe_domain"]
			self.creds = aiogoogle.auth.creds.ServiceAccountCreds(scopes=SCOPES, **credentials)
			logger.info("Loaded credentials.")
			logger.line()

	@commands.group(name="gforms")
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def gforms(self, ctx):
		"""Base group for gforms' commands.

		Some commands will require a form ID, which can be found in the url of a Google Form. (e.g. `https://docs.google.com/forms/d/<ID HERE>/`)

		Commands with flags have "-" as the prefix. (e.g. "-name arg -name arg")
		"""

	@gforms.command(brief="Set up gforms.")
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def setup(self, ctx: commands.Context, url: str = None):
		"""Gives a tutorial on how to set up gforms."""
		key_url = None

		if url:
			if not url.endswith(".json"):
				return await ctx.send("Url must be a `.json`.")
			else:
				key_url = url

		elif ctx.message.attachments:
			if len(ctx.message.attachments) > 1:
				return await ctx.send("Please only send **one** attachment.")
			if ctx.message.attachments[0].content_type != "application/json; charset=utf-8":
				return await ctx.send("Attachment must be a `.json`.")
			key_url = ctx.message.attachments[0].url

		if key_url:
			async with self.bot.session.get(key_url) as resp:
				json = await resp.json()
				try:
					jsonschema.validate(instance=json, schema=key_schema)
				except jsonschema.exceptions.ValidationError:
					return await ctx.send(
						"Attached `.json` must be a Google Cloud service account key. Use `?gforms setup` by itself for instructions."
					)
				else:
					file = await aiofiles.open(KEY_FILE, mode="wb")
					await file.write(await resp.read())
					await file.close()

					del json["universe_domain"]
					self.creds = aiogoogle.auth.creds.ServiceAccountCreds(scopes=SCOPES, **json)

					if not self.form_watch.is_running():
						self.form_watch.start()

					await self.bot.add_reaction(ctx.message, "✅")

		else:
			embeds = [
				Embed(
					title="Tutorial",
					description=(
						"Welcome to `gforms`!\n\nThis is a plugin for the Modmail Discord bot that aims to add some Google Forms"
						" interaction to your server *(p.s.: the API sucks, so there's not much)*.\n\nSee the other pages for how to get"
						" started."
					),
				),
				Embed(
					title="Create a Google Cloud project",
					description=(
						"1. Go to Google Cloud and [create a project](https://console.cloud.google.com/projectcreate).\n - Give it any"
						" name. You don't have to put an organization.\n2. Enable the Google Forms API for it"
						" [here](https://console.cloud.google.com/flows/enableapi?apiid=forms.googleapis.com)."
					),
				),
				Embed(
					title="Make a service account",
					description=(
						"1. Go [here](https://console.cloud.google.com/iam-admin/serviceaccounts).\n2. Click `Create Service Account`."
					),
				).add_field(
					name="Create service account",
					value=(
						"1. **Service account details**"
						"\n - Give it any name and description. ID is required, but one can be generated for you."
						"\n - Click `Create and Continue`."
						"\n2. **Grant this service account access to project**"
						'\n - Choose "Editor" as the role.'
						"\n - Click `Done`"
					),
				),
				Embed(
					title="Create a key",
					description=(
						"1. Click the email of your service account."
						'\n2. Go to the "Keys" tab.'
						"\n3. Click `Add Key` > `Create new key`."
						"\n4. Choose JSON as the key type."
						"\n5. Click `Create` and download the json file."
						"\nUpload the json as a __**link**__ or __**attachment**__ with the command `?gforms setup`."
					),
				),
				Embed(
					title="Finish",
					description=(
						"On any of your Google forms, in the three dots menu, click `Add collaborators` and put the email of the service"
						" account, allowing it to see and manage the form.\n\nUse `?help gforms` to see all of the `gforms` commands."
					),
				),
			]

			[e.set_author(name="gforms", icon_url="https://dem.tools/sites/default/files/2021-11/googleform.png") for e in embeds]
			paginator = GFormsPaginator(ctx, *embeds)

			await paginator.run()

	class WatchFlags(commands.FlagConverter, case_insensitive=True, delimiter=" ", prefix="-"):
		channel: Union[int, None] = commands.flag(name="channel", aliases=["ch"], description="The channel")
		hours: Union[int, float, None] = commands.flag(name="hours", description="How many hours to wait until checking")
		time: Union[str, None] = commands.flag(name="time", description="Time to wait until the first check")
		ping: Union[Tuple[discord.Member, discord.Role], None] = commands.flag(name="ping", description="A role to ping")

	@gforms.command(brief="Watch a form for responses.", usage="<form_id>")
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def watch(self, ctx: commands.Context, form_id: str = None, *, flags: WatchFlags = None):
		"""Set a watch for a Google Form, sending all responses for that form since creating the watch at your specified time.

		Setting a watch on a form already in a channel will update the other settings.
		### Flags
		- `channel/ch` - The channel to send responses to. Uses the current channel if not provided.
		- `ping` - Roles or users to ping if there are responses.
		- `hours` - How long to wait between checks. For example, passing `1` would check every hour, passing `12` would check twice a day. Values like `0.5` also work.
		- `time` - The initial time to **start** at (UTC, 24-hour). For example, you might pass `1` to `hours`, but want it to actually check on an exact hour or otherwise. Use this flag if so.
		"""
		if await is_set_up(ctx):
			now = datetime.datetime.now(datetime.timezone.utc)
			if flags and flags.channel:
				channel = await validate_channel(ctx, channel_id=channel_id)
			else:
				channel = ctx.channel

			if channel:
				if not form_id:
					return await ctx.send("Please provide the ID of the form.")
				if not flags or flags and not flags.hours:
					return await ctx.send("Please provide a period for responses to be posted with the `hours` flag.")

				if not flags.time:
					time = (now + datetime.timedelta(hours=flags.hours)).strftime("%H:%M:%S")
				else:
					try:
						time = datetime.datetime.strptime(flags.time, "%H:%M")
						time = str(time.time())
					except ValueError:
						return await ctx.send("Invalid time (should be an hour and minute in 24-hour UTC).")

				when = await get_time(time, now)

				async with aiogoogle.Aiogoogle(service_account_creds=self.creds) as aiog:
					service = await aiog.discover("forms", "v1", disco_doc_ver=2)
					form = await aiog.as_service_account(service.forms.get(formId=form_id))
					if watch := await self.db.find_one({"channel_id": channel.id, "form_id": form_id}):
						params = {"$set": {"hours": flags.hours, "when": when}}
						if flags and flags.ping:
							# I hate flags sometimes
							pings = []
							ping = ""
							for char in flags.ping:
								if char != "":
									ping = ping + char
								else:
									pings.append(ping)
									ping = ""
							params["$set"]["pings"] = pings
						if "time" in watch:
							params["$unset"] = {"time": ""}

						await self.db.update_one({"_id": watch["_id"]}, params)

					else:
						params = {
							"guild": ctx.guild.id,
							"form_title": form["info"].get("title", form["info"]["documentTitle"]),
							"form_id": form_id,
							"channel_id": channel.id,
							"hours": flags.hours,
							"since": now,
							"when": when,
						}
						if flags and flags.ping:
							params["pings"] = [mentionable.mention for mentionable in flags.ping]
						await self.db.insert_one(params)

					if self.form_watch.is_running():
						self.form_watch.restart()
					else:
						self.form_watch.start()

					await self.bot.add_reaction(ctx.message, "✅")

	@gforms.command(brief="Remove a form watch.", usage="<form id> <channel id>")
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def unwatch(self, ctx: commands.Context, form_id: str, channel_id=None):
		"""Remove a watch for a Google Form.

		Channel ID uses the current channel if not provided."""
		if await is_set_up(ctx):
			if channel_id:
				channel = channel_id
			else:
				channel = ctx.channel.id

			if channel:
				result = await self.db.delete_one({"channel_id": channel, "form_id": form_id})
				if result.deleted_count > 0:
					if self.form_watch.is_running():
						self.form_watch.restart()
					return await self.bot.add_reaction(ctx.message, "✅")
				else:
					return await ctx.send("No watch for that form in that channel.")

	@gforms.command()
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def watches(self, ctx: commands.Context):
		"""List all the form watches for the server."""
		if await is_set_up(ctx):
			watches = await self.db.find({"guild": ctx.guild.id}, {"_id": False}).to_list(None)
			embeds = []
			if watches:
				for li in listsplit(5, watches):
					embed = Embed(
						description="\n".join(
							[
								f"- **Form**: {watch['form_title']} (`{watch['form_id']}`)\n - **Channel**: <#{watch['channel_id']}>"
								f" (`{watch['channel_id']}`)\n - **Time**: {watch['time']}"
								for watch in li
							]
						)
					).set_author(icon_url=ctx.guild.icon.url, name="Form watches")
					embeds.append(embed)

				paginator = GFormsPaginator(ctx, *embeds)
				await paginator.run()
			else:
				return await ctx.send("No watches set up in this server! Use `?gforms watch` to set a watch for a form.")

	@gforms.command()
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def reset(self, ctx):
		"""Reset gforms."""
		if await is_set_up(ctx):
			if await confirmation(
				ctx, "### Are you sure you want to reset `gforms`?\n\nThis will delete your provided `.json` and watches."
			):
				os.remove(KEY_FILE)
				self.db.drop()
				await self.bot.add_reaction(ctx.message, "✅")
			else:
				await self.bot.add_reaction(ctx.message, "❎")

	class ResponsesFlags(commands.FlagConverter, case_insensitive=True, delimiter=" ", prefix="-"):
		limit: Union[int, None] = commands.flag(name="limit", aliases=["lim"], description="Only post these amount of responses")
		number: Union[int, None] = commands.flag(name="number", aliases=["num"], description="Only get the response in this position")
		time: Union[str, None] = commands.flag(name="time", description="Show only responses posted at and after this time")

	@gforms.command()
	@checks.has_permissions(checks.PermissionLevel.ADMIN)
	async def responses(self, ctx: commands.Context, form_id, *, flags: ResponsesFlags = None):
		"""Show the responses a form has.
		### Flags
		- `limit/lim` - Only post these amount of responses
		- `number/num` - Only get the response in this position
		- `time` - Show only responses posted at and after this time. Should be formatted as `YYYY-MM-DDTHH:MM:SSZ`. (e.g. 2014-10-02T15:01:23Z)
		"""
		if await is_set_up(ctx):
			nextpagetoken = None

			response_count = 0

			async with aiogoogle.Aiogoogle(service_account_creds=self.creds) as aiog:
				service = await aiog.discover("forms", "v1", disco_doc_ver=2)
				while True:
					if responses := await aiog.as_service_account(
						service.forms.responses.list(
							formId=form_id,
							pageSize=flags.limit if flags else None,
							filter=f"timestamp >= {flags.time}" if flags and flags.time else None,
							nextPageToken=nextpagetoken,
						)
					):
						for i, response in enumerate(responses["responses"], start=1):
							if flags:
								if flags.number:
									response_count += len(responses["responses"])
									if response_count >= flags.number:
										return await send_response(aiog, service, form_id, responses["responses"][flags.number - 1], ctx)
									else:
										break
							await send_response(aiog, service, form_id, response, ctx)

						if "nextPageToken" in responses:
							nextpagetoken = responses["nextPageToken"]
						else:
							break
					else:
						if flags:
							if flags.time:
								return await ctx.send("No responses since that date.")
							else:
								return await ctx.send("No responses.")

			if flags:
				if flags.number:
					return await ctx.send("This form does not have responses up to that number.")

	@gforms.command()
	@checks.has_permissions(checks.PermissionLevel.OWNER)
	async def serviceemail(self, ctx: commands.Context):
		"""Show your service account's email."""
		if await is_set_up(ctx):
			await ctx.send(view=ServiceEmailView(ctx.author.id))

	async def cog_command_error(self, ctx: commands.Context, error):
		if isinstance(error, commands.MissingRequiredArgument):
			return
		elif isinstance(error, commands.CommandInvokeError):
			if isinstance(error.original, aiogoogle.HTTPError):
				if "invalid_grant" in error.original.res.reason:
					await ctx.send(
						"The service account seems to be invalid... The stored json will be deleted. Use `?gforms setup` and use a key for"
						" a new acccount."
					)
					if self.form_watch.is_running():
						self.form_watch.cancel()
					os.remove(KEY_FILE)
					self.creds = None
				elif "The caller does not have permission" in error.original.res.reason:
					await ctx.send(
						"The provided service account does not have access to this form or the permissions needed...\nYou can show the"
						" email here for convenience."
					)
				elif "Requested entity was not found" in error.original.res.reason:
					await ctx.send("Invalid form ID.")
				elif "invalid timestamp" in error.original.res.reason:
					await ctx.send("Invalid timestamp.  See `?help gforms responses` for the proper format.")
				else:
					raise error
		else:
			raise error


async def setup(bot: ModmailBot):
	await bot.add_cog(GForms(bot))
