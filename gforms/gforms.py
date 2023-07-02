import datetime
import json
import os
from typing import Union

import aiofiles
import discord
import googleapiclient.errors
import jsonschema
from apiclient import discovery
from discord.ext import commands
from oauth2client import service_account

from bot import ModmailBot, checks
from core import paginator as pages, models

# form_service = discovery.build('forms', 'v1', http=creds.authorize(self.bot.session), discoveryServiceUrl=DISCOVERY_DOC, static_discovery=False)

SCOPES = "https://www.googleapis.com/auth/forms.body"
DISCOVERY_DOC = "https://forms.googleapis.com/$discovery/rest?version=v1"

key_file = "service_account_key.json"

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

def listsplit(num: int, li: Union[list, tuple]):
    """Makes multiple lists out of a long list.
    :param num: The number threshold until the list gets split.
    :param li: The list to split Into multiple embeds.
    :return: list
    """
    results = [li[x:x + num] for x in range(0, len(li), num)]
    return results

async def validate_channel(ctx: commands.Context, ch):
    """Validate a server channel id."""
    try:
        channel_id = int(ch)
    except ValueError:
        return await ctx.send("Invalid channel.")
    else:
        if not ctx.guild.get_channel(channel_id):
            return await ctx.send("Invalid channel.")

async def form_watch(bot: ModmailBot, time, form, channel: int):
    now = datetime.datetime.now(datetime.timezone.utc)  # Since starting watch
    while True:
        when = datetime.datetime.strptime(f"{datetime.datetime.now(datetime.timezone.utc).date()} {time}", "%Y-%m-%d %H:%M:%S").astimezone(datetime.timezone.utc)
        if when < datetime.datetime.now(datetime.timezone.utc):
            when = when + datetime.timedelta(days=1)
        await discord.utils.sleep_until(when)

        creds = service_account.ServiceAccountCredentials.from_json_keyfile_name(key_file)

        with discovery.build('forms', 'v1', credentials=creds) as service:
            _form = service.forms().get(formId=form).execute()
            if resp := service.forms().responses().list(
                    formId=form,
                    filter=f"timestamp >= {now.isoformat().replace('+00:00', 'Z')}"
            ).execute():
                await send_response(bot, channel, _form, resp)
        now = datetime.datetime.now(datetime.timezone.utc)

class Embed(discord.Embed):
    def __init__(self, color=discord.Color.from_rgb(114, 72, 185), *args, **kwargs):
        super().__init__(color=color, *args, **kwargs)

class GFormResponse:
    def __init__(self, form: dict, response: dict):
        self.embed = None
        self.form = form
        self.response = response
        self.title = None
        self.embeds = []

    async def read(self):
        self.embed = Embed(title=self.form["info"]["title"], description=self.form["info"]["description"])
        for form_item in self.form["items"]:
            try:
                if form_item.get("questionGroupItem"):
                    question_ids = [i["questionId"] for i in form_item["questionGroupItem"]["questions"]]
                else:
                    question_ids = [form_item["questionItem"]["question"]["questionId"]]
            except KeyError:
                continue
            await self.create_embeds(form_item, question_ids)
        return self

    async def create_embeds(self, item, ids: list):
        if any([i in self.response["answers"] for i in ids]):
            q_title = item.get(f"title", "(empty)") + "\n"
            q_desc = item.get(f"description", "") + "\n"

            if group_item := item.get("questionGroupItem"):
                q_answers = [
                    f"- {q['rowQuestion']['title']}\n" + "\n".join([f"  - {a['value']}" for a in self.response["answers"][q["questionId"]]["textAnswers"]["answers"]])
                    for q in group_item["questions"] if q["questionId"] in self.response["answers"]
                ]

            else:
                question_id = item["questionItem"]["question"]["questionId"]
                answers = self.response["answers"][question_id]
                if answers.get("fileUploadAnswers"):
                    q_answers = [f'- https://drive.google.com/file/d/{a["fileId"]}/view' for a in answers["fileUploadAnswers"]["answers"]]
                elif scale := item["questionItem"]["question"]["scaleQuestion"]:
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

            if 6000 <= len(q_title) + len(q_answers) + len(self.embed) or 4096 <= len(q_title) + len(q_answers) + len(self.embed.description):
                self.embeds.append(embed)
                self.embed = Embed(title=form["info"]["title"], description=form["info"]["description"])

            if isinstance(q_answers, list):
                q_answers = "\n".join(q_answers)
            self.embed.description = f"{self.embed.description}\n### {q_title}{q_desc}{q_answers}"

    async def send(self, bot: ModmailBot, channel: int):
        """Send response of a Google Form to Discord."""
        if self.embeds:
            for e in listsplit(10, self.embeds):
                await bot.guild.get_channel(channel).send(embeds=e)
        else:
            await bot.guild.get_channel(channel).send(embed=self.embed)

class GForms(commands.Cog):
    """
    Integrates Google Forms into Modmail.
    """
    def __init__(self, bot):
        self.bot: ModmailBot = bot
        self.db = bot.api.get_plugin_partition(self)

    @commands.group(name="gforms")
    @checks.has_permissions(checks.PermissionLevel.ADMIN)
    async def gforms(self, ctx):
        """Base group for gforms's commands."""

    @gforms.command(brief="Set up gforms.")
    @checks.has_permissions(checks.PermissionLevel.ADMIN)
    async def setup(self, ctx: commands.Context, url: str = None):
        """Gives a tutorial on how to set up gforms."""
        if not os.path.exists(key_file):
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
                async with self.bot.session.get(url) as resp:
                    json = await resp.json()
                    try:
                        jsonschema.validate(instance=json, schema=key_schema)
                    except jsonschema.exceptions.ValidationError:
                        return await ctx.send("Attached `.json` must be a Google Cloud service account key. Use `?gforms setup` for instructions.")
                    else:
                        f = await aiofiles.open(key_file, mode='wb')
                        await f.write(await resp.read())
                        await f.close()
                        return await ctx.send("`gforms` setup complete!")

            else:
                embeds = [
                    Embed(
                        title="Tutorial",
                        description="Welcome to `gforms`!"
                        "\n\nThis is a plugin for the Modmail Discord bot that posts Google Forms recieved."
                        "\n\nSee the other pages for how to get started."
                    ),
                    Embed(
                        title="Create the Google Cloud project",
                        description="1. Go to Google Cloud and [create a project](https://console.cloud.google.com/projectcreate)."
                        "\n - Give it any name. You don't have to put an organization."
                        "\n2. Enable the Google Forms API for it [here](https://console.cloud.google.com/flows/enableapi?apiid=forms.googleapis.com)."
                    ),
                    Embed(
                        title="Configure OAuth",
                        description="- Go [here](https://console.cloud.google.com/apis/credentials/consent)."
                        "\n - Select **External** user type."
                        "\n - Click `Create`."
                    ).add_field(
                        name="Edit app registration",
                        value="1. **OAuth consent screen**"
                        "\n - Set the required fields (App name/User support email/Developer contact information) to whatever."
                        "\n - Click `Save and Continue`."
                        "\n2. **Scopes**"
                        "\n - You don't have to do anything here. Click `Save and continue`."
                        "\n3. **Test users**"
                        "\n - Click `Add users` and add the email of the Google account(s) containing the forms you'd like to use for this plugin."
                        "\n - Click `Save and Continue`."
                        "\n4. **Summary**"
                        "\n - Click `Back to Dashboard`."
                    ),
                    Embed(
                        title="Get credentials",
                        description="- Go [here](https://console.cloud.google.com/apis/credentials)."
                        "\n - Click `Create Credentials` > `OAuth client ID`."
                        "\n - Select \"Desktop app\" as the application type."
                        "\n - Give whatever name."
                        "\n - Click `Create`."
                        "\n- You'll get a popup saying \"OAuth client created\". Download the json and upload it as a **link** or **attachment** with the command `?gforms setup`."
                    ),
                    Embed(
                        title="Finish",
                        description="You're done!\n\nUse `?help gforms` to see all of the `gforms` commands."
                    )

                ]

                [e.set_author(name="gforms", icon_url="https://dem.tools/sites/default/files/2021-11/googleform.png") for e in embeds]

                paginator = pages.EmbedPaginatorSession(ctx, *embeds)

                await paginator.run()
        else:
            return await ctx.send("`gforms` has been set up for this server already. Use `?gforms reset` to start over.")

    @gforms.command(usage="<form_id> <channel id> <hours:minutes>")
    @checks.has_permissions(checks.PermissionLevel.ADMIN)
    async def watch(self, ctx: commands.Context, form_id: str, channel_id: int, *, time: str):
        """Set a watch for a Google Form."""
        if await self.db.find_one({"channel_id": channel_id, "form_id": form_id}, {"_id": 0}):
            return await ctx.send("A watch already exists for this form in that channel. You can remove a watch with `?gforms unwatch`.")

        try:
            time = datetime.datetime.strptime(time, "%H:%M")
        except ValueError:
            return await ctx.send("Invalid time (should be in 24-hour format).")
        else:
            time = str(time.time())

        await validate_channel(ctx, channel_id)

        creds = service_account.ServiceAccountCredentials.from_json_keyfile_name(key_file)

        with discovery.build('forms', 'v1', credentials=creds) as service:
            try:
                service.forms().get(formId=form_id).execute()  # validate the form
            except googleapiclient.errors.HttpError:
                return await ctx.send("Invalid form ID.")
            else:
                task = self.bot.loop.create_task(form_watch(self.bot, time, form_id, channel_id))
                await self.db.insert_one({
                    "form_id": form_id,
                    "channel_id": channel_id,
                    "time": time,
                    "task": task
                })
                await self.bot.add_reaction(ctx.message, "✅")
                await task

    @gforms.command(usage="<form_id> <channel id>")
    @checks.has_permissions(checks.PermissionLevel.ADMIN)
    async def unwatch(self, ctx: commands.Context, form_id: str, channel_id: int):
        """Remove a watch for a Google Form."""
        await validate_channel(ctx, channel_id)

        if await self.db.delete_one({"channel_id": channel_id, "form_id": form_id}):
            return self.bot.add_reaction(ctx.message, "✅")
        else:
            return await ctx.send("No watch for that form in that channel.")

    @gforms.command(hidden=True)
    @checks.has_permissions(checks.PermissionLevel.OWNER)
    async def responses(self, ctx: commands.Context, form, time=None):
        """Show the responses a form has."""
        creds = service_account.ServiceAccountCredentials.from_json_keyfile_name(key_file)

        with discovery.build('forms', 'v1', credentials=creds) as service:
            try:
                if response_list := service.forms().responses().list(
                        formId=form,
                        filter=time
                ).execute():
                    for r in response_list["responses"]:
                        messages = await GFormResponse(service.forms().get(formId=form).execute(), r).read()
                        await messages.send(self.bot, ctx.channel.id)
            except googleapiclient.errors.HttpError:
                return await ctx.send("Invalid form ID.")

    @gforms.command()
    @checks.has_permissions(checks.PermissionLevel.OWNER)
    async def testo(self, ctx):
        """Test command."""

    async def cog_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            return
        if isinstance(error.original, (FileNotFoundError, json.JSONDecodeError)):
            await ctx.send("No service account json found. Use `?gforms setup`.", reference=ctx.message)
        else:
            raise error


async def setup(bot: ModmailBot):
    db = bot.api.get_plugin_partition(GForms(bot))
    async for watch in db.find({}, {"_id": 0}):
        task = bot.loop.create_task(form_watch(bot, watch["time"], watch["form_id"], watch["channel_id"]))
        await db.update_one({"form_id": watch["form_id"], "channel_id": watch["channel_id"]}, {"task": task})
        await task

    await bot.add_cog(GForms(bot))
