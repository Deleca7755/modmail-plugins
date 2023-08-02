import datetime
import json
import os
from typing import Union

import aiofiles
import re
import discord
import googleapiclient.errors
import jsonschema
import motor.core
from apiclient import discovery
from discord.ext import commands, tasks
from oauth2client import service_account, client

from bot import ModmailBot, checks
from core import paginator as pages

SCOPES = "https://www.googleapis.com/auth/forms.body"
DISCOVERY_DOC = "https://forms.googleapis.com/$discovery/rest?version=v1"

KEY_FILE = "service_account_key.json"

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
    "required": ["type", "project_id", "private_key_id", "client_email", "client_id", "auth_uri", "token_uri", "auth_provider_x509_cert_url", "client_x509_cert_url", "universe_domain"]

}

class Embed(discord.Embed):
    def __init__(self, color=discord.Color.from_rgb(114, 72, 185), *args, **kwargs):
        super().__init__(color=color, *args, **kwargs)

class ConfirmView(discord.ui.View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.value = None

    @discord.ui.button(
        label="Yes",
        style=discord.ButtonStyle.green
    )
    async def y(self, interaction: discord.Interaction, button):
        await interaction.message.delete()
        self.value = True
        self.stop()

    @discord.ui.button(
        label="No",
        style=discord.ButtonStyle.red
    )
    async def n(self, interaction: discord.Interaction, button):
        await interaction.message.delete()
        self.value = False
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

async def is_set_up(r=True):
    if not os.path.exists(KEY_FILE):
        if r:
            raise FileNotFoundError
        else:
            return False
    else:
        return True

async def validate_channel(ctx: commands.Context, ch):
    """Validate a server channel id."""
    try:
        channel_id = int(ch)
    except ValueError:
        return await ctx.send("Invalid channel.")
    else:
        if not ctx.guild.get_channel(channel_id):
            return await ctx.send("Invalid channel.")

def listsplit(num: int, li: Union[list, tuple]):
    """Makes multiple lists out of a long list.
    :param num: The number threshold until the list gets split.
    :param li: The list to split Into multiple embeds.
    :return: list
    """
    results = [li[x:x + num] for x in range(0, len(li), num)]
    return results

class GFormResponse:
    def __init__(self, form: dict, response: dict):
        self.form = form
        self.response = response
        self.title = None
        self.answers = None
        self._embed = None
        self._embeds = []

    async def read(self):
        time = datetime.datetime.strptime(re.sub("\..+?(?=Z)", "", self.response["lastSubmittedTime"]), "%Y-%m-%dT%H:%M:%SZ")
        self._embed = Embed(
            title=self.form["info"].get("title", self.form["info"]["documentTitle"]),
            description=self.form["info"].get("description", ""),
            timestamp=time
        ).set_footer(text=f'Response ID {self.response["responseId"]}')

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
            await self.create_embeds(form_item, question_ids)
        if self._embeds:
            if self._embed not in self._embeds:
                self._embeds.append(self._embed)
        return self

    async def create_embeds(self, item, ids: list):
        """Generate embeds from a Google Form response."""
        if self.answers:
            if any([i in self.answers for i in ids]):
                q_title = item.get(f"title", "(empty)") + "\n"
                q_desc = item.get(f"description", "") + "\n"

                if group_item := item.get("questionGroupItem"):
                    q_answers = [
                        f"- {q['rowQuestion']['title']}\n" + "\n".join([f"  - {a['value']}" for a in self.answers[q["questionId"]]["textAnswers"]["answers"]])
                        for q in group_item["questions"] if q["questionId"] in self.answers
                    ]

                else:
                    question_id = item["questionItem"]["question"]["questionId"]
                    answers = self.answers[question_id]
                    if answers.get("fileUploadAnswers"):
                        q_answers = [f'- https://drive.google.com/file/d/{a["fileId"]}/view' for a in answers["fileUploadAnswers"]["answers"]]
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

                        low_label = scale.get('lowLabel')
                        high_label = scale.get('highLabel')
                        q_answers = "{} {} {} {} {}".format(
                            f"*{low_label}* — " if low_label is not None else '',
                            low or "0",
                            counter,
                            high,
                            f" — *{high_label}*" if high_label is not None else '')

                    else:
                        q_answers = [f'- {a["value"]}' for a in answers["textAnswers"]["answers"]]

                if isinstance(q_answers, list):
                    q_answers = "\n".join(q_answers)

                if 6000 <= len(q_title) + len(q_answers) + len(self._embed) or 4096 <= len(q_title) + len(q_answers) + len(self._embed.description):
                    self._embeds.append(self._embed)
                    self._embed = Embed().set_footer(text=f'Response ID {self.response["responseId"]}')

                self._embed.description = f"{self._embed.description}\n### {q_title}{q_desc}{q_answers}"

    async def send(self, bot: ModmailBot, channel: int):
        """Send response of a Google Form to Discord."""
        if self._embeds:
            for e in listsplit(10, self._embeds):
                await bot.guild.get_channel(channel).send(embeds=e)
        else:
            await bot.guild.get_channel(channel).send(embed=self._embed)

class GForms(commands.Cog):
    """Integrates Google Forms into Modmail."""
    def __init__(self, bot):
        self.bot: ModmailBot = bot
        self.db: motor.core.AgnosticCollection = bot.api.get_plugin_partition(self)

    @tasks.loop
    async def form_watch(self):
        task = None

        query = self.db.find().sort([("when", 1)]).limit(1)

        async for watch in query:
            task = watch

        if task is None:
            return self.form_watch.cancel()

        await discord.utils.sleep_until(task["when"].replace(tzinfo=datetime.timezone.utc))

        creds = service_account.ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE)

        since = task['since'].replace(tzinfo=datetime.timezone.utc)

        with discovery.build("forms", "v1", credentials=creds) as service:
            _form = service.forms().get(formId=task["form_id"]).execute()
            title = task["form_title"]
            if resp := service.forms().responses().list(formId=task["form_id"], filter=f"timestamp >= {since.isoformat().replace('+00:00', 'Z')}").execute():
                count = len(resp['responses'])
                if count > 1:
                    msg = f"**{title}**: **{count}** new responses since {since.strftime('%c')} :arrow_heading_down:"
                else:
                    msg = f"**{title}**: **{count}** new response since {since.strftime('%c')} :arrow_heading_down:"
                await self.bot.guild.get_channel(task["channel_id"]).send(msg)
                for r in resp["responses"]:
                    messages = await GFormResponse(service.forms().get(formId=task["form_id"]).execute(), r).read()
                    await messages.send(self.bot, task["channel_id"])
            else:
                await self.bot.guild.get_channel(task["channel_id"]).send(f"**{title}**: No responses have been submitted since {since.strftime('%c')}.")

        now = datetime.datetime.now(datetime.timezone.utc)
        when = datetime.datetime.strptime(f"{datetime.datetime.now(datetime.timezone.utc).date()} {task['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        if when < datetime.datetime.now(datetime.timezone.utc):
            when = when + datetime.timedelta(days=1)

        await self.db.update_one(
            {"_id": task["_id"]},
            {
                "$set": {
                    "since": now,
                    "when": when
                }
            },
            upsert=False
        )

    @form_watch.before_loop
    async def watch_before(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        self.form_watch.start()

    @commands.group(name="gforms")
    @checks.has_permissions(checks.PermissionLevel.ADMIN)
    async def gforms(self, ctx):
        """Base group for gforms's commands.

        Some commands will require a form ID, which can be found in the url of a Google Form (e.g. `https://docs.google.com/forms/d/<ID HERE>/`)"""

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
                    return await ctx.send("Attached `.json` must be a Google Cloud service account key. Use `?gforms setup` by itself for instructions.")
                else:
                    f = await aiofiles.open(KEY_FILE, mode='wb')
                    await f.write(await resp.read())
                    await f.close()

                    if not self.form_watch.is_running():
                        self.form_watch.start()

                    await self.bot.add_reaction(ctx.message, "✅")

        else:
            embeds = [
                Embed(
                    title="Tutorial",
                    description="Welcome to `gforms`!"
                                "\n\nThis is a plugin for the Modmail Discord bot that aims to add some Google Forms interaction for your Discord."
                                "\n\nSee the other pages for how to get started."
                ),
                Embed(
                    title="Create a Google Cloud project",
                    description="1. Go to Google Cloud and [create a project](https://console.cloud.google.com/projectcreate)."
                                "\n - Give it any name. You don't have to put an organization."
                                "\n2. Enable the Google Forms API for it [here](https://console.cloud.google.com/flows/enableapi?apiid=forms.googleapis.com)."
                ),
                Embed(
                    title="Make a service account",
                    description="1. Go [here](https://console.cloud.google.com/iam-admin/serviceaccounts)."
                                "\n2. Click `Create Service Account`."

                ).add_field(
                    name="Create service account",
                    value="1. **Service account details**"
                          "\n - Give it any name and description. ID is required, but one can be generated for you."
                          "\n - Click `Create and Continue`."
                          "\n2. **Grant this service account access to project**"
                          "\n - Choose \"Editor\" as the role."
                          "\n - Click `Done`"
                ),
                Embed(
                    title="Create a key",
                    description="1. Click the email of your service account."
                                "\n2. Go to the \"Keys\" tab."
                                "\n3. Click `Add Key` > `Create new key`."
                                "\n4. Choose JSON as the key type."
                                "\n5. Click `Create` and download the json file."
                                "\nUpload the json as a **link** or **attachment** with the command `?gforms setup`"
                ),
                Embed(
                    title="Finish",
                    description="On any of your Google forms, in the three dots menu, click `Add collaborators` and put the email of the service account, allowing it to see and manage the form."
                                "\n\nUse `?help gforms` to see all of the `gforms` commands."
                )

            ]

            [e.set_author(name="gforms", icon_url="https://dem.tools/sites/default/files/2021-11/googleform.png") for e in embeds]

            paginator = pages.EmbedPaginatorSession(ctx, *embeds)

            await paginator.run()

    @gforms.command(brief="Watch a form for responses.", usage="<form id> <channel id> <hour:minutes>")
    @checks.has_permissions(checks.PermissionLevel.ADMIN)
    async def watch(self, ctx: commands.Context, form_id: str, channel_id: int, *, time: str):
        """Set a watch for a Google Form, sending all responses for that form since creating the watch every day at a specified hour (UTC, 24-hour)."""
        await is_set_up()

        if await self.db.find_one({"channel_id": channel_id, "form_id": form_id}, {"_id": 0}):
            return await ctx.send("A watch already exists for this form in that channel. You can remove a watch with `?gforms unwatch`.")

        await validate_channel(ctx, channel_id)

        try:
            time = datetime.datetime.strptime(time, "%H:%M")
        except ValueError:
            return await ctx.send("Invalid time (should be an hour and minute in 24-hour UTC).")
        else:
            time = str(time.time())

        creds = service_account.ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE)

        with discovery.build('forms', 'v1', credentials=creds) as service:
            form = service.forms().get(formId=form_id).execute()

            now = datetime.datetime.now(datetime.timezone.utc)
            when = datetime.datetime.strptime(f"{datetime.datetime.now(datetime.timezone.utc).date()} {time}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
            if when < datetime.datetime.now(datetime.timezone.utc):
                when = when + datetime.timedelta(days=1)

            await self.db.insert_one({
                "form_title": form["info"].get("title", form["info"]["documentTitle"]),
                "form_id": form_id,
                "channel_id": channel_id,
                "time": time,
                "since": now,
                "when": when
            })

            if self.form_watch.is_running():
                self.form_watch.restart()
            else:
                self.form_watch.start()

            await self.bot.add_reaction(ctx.message, "✅")

    @gforms.command(brief="Remove a form watch.", usage="<form id> <channel id>")
    @checks.has_permissions(checks.PermissionLevel.ADMIN)
    async def unwatch(self, ctx: commands.Context, form_id: str, channel_id: int):
        """Remove a watch for a Google Form."""
        await is_set_up()

        await validate_channel(ctx, channel_id)

        result = await self.db.delete_one({"channel_id": channel_id, "form_id": form_id})
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
        await is_set_up()

        watches = await self.db.find({}, {'_id': False}).to_list(None)
        embeds = []
        if watches:
            for li in listsplit(5, watches):
                embed = Embed(
                    title="Form watches",
                    description="\n".join(
                        [
                            f"{num}. _ _\n - **Form**: {watch['form_id']}\n - **Channel**: <#{watch['channel_id']}> (`{watch['channel_id']}`)\n - **Time**: {watch['time']}" for num, watch in enumerate(li)
                        ]
                    )
                )
                embeds.append(embed)

            paginator = pages.EmbedPaginatorSession(ctx, *embeds)
            await paginator.run()
        else:
            return await ctx.send("No watches set up! Use `?gforms watch` to set a watch for a form.")

    @gforms.command()
    @checks.has_permissions(checks.PermissionLevel.ADMIN)
    async def reset(self, ctx):
        """Reset gforms."""
        await is_set_up()

        if await confirmation(
                ctx,
                "### Are you sure you want to reset `gforms`?\n\n"
                "This will delete your provided `.json` and watches."
        ):
            os.remove(KEY_FILE)
            self.db.drop()
            await self.bot.add_reaction(ctx.message, "✅")
        else:
            await self.bot.add_reaction(ctx.message, "❎")

    class ResponsesFlags(commands.FlagConverter, case_insensitive=True, delimiter=" ", prefix="-"):
        limit: Union[int, None] = commands.flag(name="limit", aliases=['lim', 'num'], description="Only post these amount of responses")
        time: Union[int, None] = commands.flag(name="time", description="Show only responses posted after this time")

    @gforms.command(hidden=True)
    @checks.has_permissions(checks.PermissionLevel.OWNER)
    async def responses(self, ctx: commands.Context, form_id, *, flags: ResponsesFlags = None):
        """Show the responses a form has."""
        creds = service_account.ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE)

        with discovery.build('forms', 'v1', credentials=creds) as service:
            if response_list := service.forms().responses().list(
                    formId=form_id,
                    filter=flags.time if flags else None
            ).execute():
                for number, r in enumerate(response_list["responses"], start=1):
                    messages = await GFormResponse(service.forms().get(formId=form_id).execute(), r).read()
                    await messages.send(self.bot, ctx.channel.id)
                    if number == flags.limit:
                        return
            else:
                return await ctx.send("No responses.")

    @gforms.command(hidden=True)
    @checks.has_permissions(checks.PermissionLevel.OWNER)
    async def testo(self, ctx):
        """Test command."""

    async def cog_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            return
        if isinstance(error.original, (FileNotFoundError, json.JSONDecodeError)):
            return await ctx.send("`gforms` has not yet been set up. Use `?gforms setup`.", reference=ctx.message)
        if isinstance(error.original, client.HttpAccessTokenRefreshError):
            await ctx.send("The service account seems to be invalid... The stored json will be deleted. Use `?gforms setup` and use a key for a new acccount.")
            if self.form_watch.is_running():
                self.form_watch.cancel()
            os.remove(KEY_FILE)
        if isinstance(error.original, googleapiclient.errors.HttpError):
            if error.original.reason == 'The caller does not have permission':
                await ctx.send("The provided service account does not have access to this form or the permissions needed.")
            if error.original.reason == 'Requested entity was not found':
                await ctx.send("Invalid form ID.")
        else:
            raise error


async def setup(bot: ModmailBot):
    await bot.add_cog(GForms(bot))
