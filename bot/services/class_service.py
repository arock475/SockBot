import logging

import discord
from discord import CategoryChannel

import bot.bot_secrets as bot_secrets
from bot.consts import Colors
from bot.data.class_repository import ClassRepository
from bot.messaging.events import Events
from bot.models.class_models import ClassSemester, ClassChannel, ClassChannelScaffold
from bot.services.base_service import BaseService
from bot.sock_bot import SockBot
from bot.utils.helpers import error_embed, fetch_optional_message

MAX_CHANNELS_PER_CATEGORY = 50
WELCOME_MESSAGE_REACTION = '✅'

log = logging.getLogger(__name__)


class ClassService(BaseService):

    def __init__(self, bot: SockBot):
        super().__init__(bot)
        self.messages: set[int] = set()
        self.repo = ClassRepository()

    @BaseService.listener(Events.on_class_create)
    async def on_class_create(self, inter: discord.Interaction, cls: ClassChannelScaffold, desc: str | None = None):
        if not (semester := await self._check_semester(inter)):
            return
        await inter.response.defer(thinking=True)

        # create and assign what we couldn't in the class channel scaffold
        category = await self._get_or_create_category(cls)
        role = await self._get_or_create_role(cls)
        channel = await self.bot.guild.create_text_channel(name=cls.channel_name, topic=f'{cls.class_name} - {desc}')
        class_channel = ClassChannel(cls.class_prefix, cls.class_number, cls.class_professor,
                                     cls.class_name, channel.id, semester.semester_id,
                                     category.id, role.id, None, None)

        # move, sort, sync permissions, and push the new class to the repo
        await self._move_and_sort(category, channel)
        await self._sync_perms(class_channel)
        await channel.send(content=inter.user.mention)
        await self._send_welcome(class_channel, True, inter.user)
        await self.repo.insert_class(class_channel)

        # add the class role to the given user and prepare our embed
        await inter.user.add_roles(role, reason='Class creation')
        embed = discord.Embed(title='📔 Class Created', color=Colors.Purple)
        embed.description = f'Your new class channel has been created: {channel.mention}'
        embed.add_field(name='Class Title', value=cls.full_title, inline=False)
        embed.add_field(name='Semester', value=semester.semester_name)
        embed.add_field(name='Instructor', value=cls.class_professor)
        embed.add_field(name='Created By', value=inter.user.mention)

        # send our embed to the notifications channel + to the user
        await self._get_notifs_channel().send(embed=embed)
        await inter.followup.send(embed=embed)

    @BaseService.listener(Events.on_class_insert)
    async def on_class_insert(self,
                              inter: discord.Interaction,
                              cls: ClassChannelScaffold,
                              channel: discord.TextChannel,
                              role: discord.Role | None = None,
                              desc: str | None = None,
                              archive: bool = False):
        if not (semester := await self._check_semester(inter)):
            return
        await inter.response.defer(thinking=True)

        # create and assign what we couldn't in the class channel scaffold
        category = self._available_archive_category() if archive else await self._get_or_create_category(cls)
        class_role: discord.Role | None = None
        if not archive:
            class_role = role if role else await self._get_or_create_role(cls)
        class_channel = ClassChannel(class_prefix=cls.class_prefix, class_number=cls.class_number,
                                     class_professor=cls.class_professor, class_name=cls.class_name,
                                     channel_id=channel.id, semester_id=semester.semester_id, category_id=category.id,
                                     class_role_id=class_role.id if class_role else None, class_ta_role_id=None,
                                     post_message_id=None, class_archived=archive)

        # move, sort, sync permissions, and push the new class to the repo
        await channel.edit(name=cls.channel_name, topic=f'{cls.class_name} - {desc}')
        await self._move_and_sort(category, channel)
        await self._sync_perms(class_channel)
        if not archive:
            await self._send_welcome(class_channel, False, inter.user)
        await self.repo.insert_class(class_channel)

        # prepare our embed and include whether we set a role for the class channel
        embed = discord.Embed(title='📔 Class Inserted', color=Colors.Purple)
        embed.description = f'The channel {channel.mention} has been inserted as a class.'
        if role:
            embed.description += f'\nThe role {role.mention} has been set as the class role.'
        embed.add_field(name='Class Title', value=cls.full_title, inline=False)
        embed.add_field(name='Semester', value=semester.semester_name)
        embed.add_field(name='Instructor', value=cls.class_professor)
        embed.add_field(name='Archived', value=str(archive))
        embed.add_field(name='Inserted By', value=inter.user.mention)

        # send our embed to the notifications channel + to the user
        await self._get_notifs_channel().send(embed=embed)
        await inter.followup.send(embed=embed)

    @BaseService.listener(Events.on_class_edit)
    async def on_class_edit(self,
                            inter: discord.Interaction,
                            cls: ClassChannelScaffold,
                            channel: discord.TextChannel,
                            role: discord.Role | None = None,
                            desc: str | None = None):
        if not (cc := await self.repo.search_class_by_channel(channel)):
            return
        await inter.response.defer(thinking=True)

        cc.class_prefix = cls.class_prefix
        cc.class_number = cls.class_number
        cc.class_professor = cls.class_professor
        cc.class_name = cls.class_name
        if role:
            cc.class_role_id = role.id

        if not role:
            role = self.bot.guild.get_role(cc.class_role_id)

        category = self._available_archive_category() if cc.class_archived else await self._get_or_create_category(cls)
        await channel.edit(name=cls.channel_name, topic=f'{cls.class_name} - {desc}')
        await self._move_and_sort(category, channel)
        await self._sync_perms(cc)
        await self.repo.update_class(cc)

        embed = discord.Embed(title='📔 Class Edited', color=Colors.Purple)
        embed.add_field(name='Class Title', value=cc.full_title, inline=False)
        embed.add_field(name='Instructor', value=cc.class_professor)
        embed.add_field(name='Role', value=role.mention if role else 'None')
        if desc:
            embed.add_field(name='Description', value=desc)
        embed.add_field(name='Edited by', value=inter.user.mention)

        # send our embed to the notifications channel + to the user
        await self._get_notifs_channel().send(embed=embed)
        await inter.followup.send(embed=embed)

    @BaseService.listener(Events.on_semester_archive)
    async def on_semester_archive(self, semester: ClassSemester, inter: discord.Interaction | None = None):
        # go through each channel we have in the semester that is unarchived and archive it
        if inter:
            await inter.response.defer(thinking=True)
        channels = await self.repo.get_semester_classes(semester)
        notif_channel = self._get_notifs_channel()
        channel_mentions = []
        for channel in channels:
            for class_ta in await self.repo.get_tas_by_channel(channel.channel_id):
                await self.repo.delete_ta(class_ta)
            if class_role := self.bot.guild.get_role(channel.class_role_id):
                await class_role.delete(reason='Semester archival')
            if ta_role := self.bot.guild.get_role(channel.class_ta_role_id):
                await ta_role.delete(reason='Semester archival')
            channel_mentions.append(await self.on_class_archive(channel))

        # prepare our embed
        embed = discord.Embed(title='📔 Semester Archived', color=Colors.Purple)
        embed.add_field(name='Semester', value=semester.semester_name)
        embed.add_field(name='Class Count', value=len(channels))
        embed.add_field(name='Archived By', value=inter.user.mention if inter else 'System')
        embed.add_field(name='Channels Archived', value='\n'.join(channel_mentions))

        # send our embed to the notifications channel + to the user
        await notif_channel.send(embed=embed)
        if inter:
            await inter.followup.send(embed=embed, ephemeral=True)

    @BaseService.listener(Events.on_class_archive)
    async def on_class_archive(self, cls: ClassChannel, inter: discord.Interaction | None = None) -> str | None:
        if inter:
            await inter.response.defer(thinking=True)
        # we will fail if we cannot move our channel
        if not (category := self._available_archive_category()):
            await self._send_failure(cls, 'Class Archive Failed', 'No archival category to move to.')
            return None

        # make sure channel exists
        if not (channel := self.bot.guild.get_channel(cls.channel_id)):
            await self._send_failure(cls, 'Class Archive Failed', f'Channel with ID `{cls.channel_id}` not found.')
            return None

        # move, sort, edit channel permissions, and update the class channel in our repo
        await self._move_and_sort(category, channel)
        await channel.set_permissions(self.bot.guild.default_role, view_channel=False)
        cls.class_archived = True
        cls.category_id = category.id
        await self.repo.update_class(cls)
        if cls.post_message_id in self.messages:
            self.messages.remove(cls.post_message_id)

        # prepare our embed
        embed = discord.Embed(title='📔 Class Archived', color=Colors.Purple)
        embed.add_field(name='Class Title', value=cls.full_title, inline=False)
        embed.add_field(name='Channel', value=channel.mention)
        embed.add_field(name='Moved To', value=category.name)
        embed.add_field(name='Archived By', value=inter.user.mention if inter else 'System', inline=False)

        # send our embed to the notifications channel, the channel the command was sent in, and to the user
        await channel.send(embed=embed)
        if inter:
            await inter.followup.send(embed=embed)
        await self._get_notifs_channel().send(embed=embed)
        # return the mention of the channel being archived, used by self.on_semester_archive()
        return channel.mention

    @BaseService.listener(Events.on_class_unarchive)
    async def on_class_unarchive(self, inter: discord.Interaction, cls: ClassChannel):
        if not (semester := await self._check_semester(inter)):
            return None
        await inter.response.defer(thinking=True)

        # make sure the channel exists before we unarchive
        if not (channel := self.bot.guild.get_channel(cls.channel_id)):
            await self._send_failure(cls, 'Class Unarchive Failed', f'Channel with ID `{cls.channel_id}` not found.')
            return None

        # get our category and role ready
        category = await self._get_or_create_category(cls)
        role = await self._get_or_create_role(cls)

        # move, sort, sync perms, update the class channel, send our welcome message, and update the repo
        await self._move_and_sort(category, channel)
        await self._sync_perms(cls)
        cls.class_archived = False
        cls.category_id = category.id
        cls.class_role_id = role.id
        cls.semester_id = semester.semester_id
        await channel.send(content=inter.user.mention)
        await self._send_welcome(cls, False, inter.user)
        await self.repo.update_class(cls)

        # add the new or already-existing role to the user and prepare our embed
        await inter.user.add_roles(role, reason='Class unarchival')
        embed = discord.Embed(title='📔 Class Unarchived', color=Colors.Purple)
        embed.description = f'Your class channel has been unarchived: {channel.mention}'
        embed.add_field(name='Class Title', value=cls.full_title, inline=False)
        embed.add_field(name='Semester', value=semester.semester_name)
        embed.add_field(name='Instructor', value=cls.class_professor)
        embed.add_field(name='Unarchived By', value=inter.user.mention)

        # send our embed to the notifications channel + to the user
        await inter.followup.send(embed=embed)
        await self._get_notifs_channel().send(embed=embed)

    @BaseService.listener(Events.on_guild_channel_delete)
    async def on_channel_delete(self, channel: discord.TextChannel):
        # check if the deleted channel was a class channel
        if not (class_channel := await self.repo.search_class_by_channel(channel)):
            return

        # remove the post message ID from our cache and delete the role
        if not class_channel.class_archived:
            self.messages.remove(class_channel.post_message_id)

        # push the deletion to our repository, prepare our embed, and mention if we deleted a role
        await self.repo.delete_class(class_channel)
        embed = discord.Embed(title='📔 Class Channel Deleted', color=Colors.Error)
        embed.description = f'Class channel #{class_channel.channel_name} deleted.'
        embed.add_field(name='Class Title', value=class_channel.full_title)
        embed.add_field(name='Semester', value=class_channel.semester_id)

        # send our embed to the notifications channel
        await self._get_notifs_channel().send(embed=embed)

    @BaseService.listener(Events.on_guild_role_delete)
    async def on_role_delete(self, role: discord.Role):
        if not (class_channel := await self.repo.search_class_by_role(role)):
            return
        # we should not be calling update_class if the role is being deleted due to channel deletion (see above func)
        # we can check whether this is due to channel deletion or role deletion by checking our cache
        # if the channel is being deleted, self.messages should not contain the post_message_id for the class channel
        if class_channel.post_message_id in self.messages:
            class_channel.class_role_id = None
            self.messages.remove(class_channel.post_message_id)
            await self.repo.update_class(class_channel)
        elif class_channel.class_ta_role_id == role.id:
            class_channel.class_ta_role_id = None
            await self.repo.update_class(class_channel)

    @BaseService.listener(Events.on_reaction_add)
    async def on_reaction_add(self, react: discord.Reaction, user: discord.Member):
        if react.emoji != WELCOME_MESSAGE_REACTION:
            return
        if user.bot or react.message.id not in self.messages:
            return
        if not (class_channel := await self.repo.search_class_by_channel(react.message.channel)):
            return
        if not (role := self.bot.guild.get_role(class_channel.class_role_id)):
            return
        await user.add_roles(role, reason='Class channel reaction add.')

    @BaseService.listener(Events.on_reaction_remove)
    async def on_reaction_remove(self, react: discord.Reaction, user: discord.Member):
        if react.emoji != WELCOME_MESSAGE_REACTION:
            return
        if user.bot or react.message.id not in self.messages:
            return
        if not (class_channel := await self.repo.search_class_by_channel(react.message.channel)):
            return
        if not (role := self.bot.guild.get_role(class_channel.class_role_id)):
            return
        await user.remove_roles(role, reason='Class channel reaction remove.')

    @BaseService.listener(Events.on_message_delete)
    async def on_message_delete(self, message: discord.Message):
        if message.id in self.messages:
            self.messages.remove(message.id)

    async def _move_and_sort(self, category: CategoryChannel, channel: discord.TextChannel):
        """
        Moves the given discord.TextChannel to the given discord.CategoryChannel.
        Sorts the channel based on descending order and syncs the permissions of the channel based off the category.
        """
        i = len([ch for ch in category.channels if isinstance(ch, discord.TextChannel) and ch.name < channel.name])
        await channel.move(beginning=True, offset=i, category=category, sync_permissions=True)

    def _available_archive_category(self) -> CategoryChannel | None:
        """
        Gets an archive category that has room for at least one channel.
        If None is returned, there are no archive categories that are NOT full.
        """
        archive_categories = bot_secrets.secrets.class_archive_category_ids
        for category in self.bot.guild.categories:
            if category.id not in archive_categories:
                continue
            if len(category.channels) < MAX_CHANNELS_PER_CATEGORY:
                return category
        return None

    async def _get_or_create_category(self, cls: ClassChannelScaffold) -> CategoryChannel:
        """
        Gets or creates a new category channel for the given scaffold.
        """
        category_name = cls.intended_category
        for category in self.bot.guild.categories:
            if category_name == category.name:
                return category
        return await self.bot.guild.create_category(name=category_name)

    async def _get_or_create_role(self, cls: ClassChannelScaffold) -> discord.Role:
        """
        Gets or creates a new role for the given class scaffold.
        """
        for r in self.bot.guild.roles:
            if r.name == cls.class_code:
                return r
        return await self.bot.guild.create_role(
            name=cls.class_code,
            mentionable=True,
            reason=f'Class creation or could not find class role {cls.class_code}'
        )

    async def _sync_perms(self, cls: ClassChannel) -> None:
        """
        Syncs the permissions for the given class channel.
        This method is specifically for class channels that are being created, inserted, or unarchived.

        The following permissions are set, for the given roles:
        CLASS ROLE      POSITION 3      VIEW_CHANNEL = TRUE
        CLEANUP ROLE    POSITION 2      VIEW_CHANNEL = FALSE
        """
        role = await self._get_or_create_role(cls)
        cleanup = await self._get_or_create_cleanup()
        if not (channel := self.bot.guild.get_channel(cls.channel_id)):
            await self._send_failure(cls, 'Syncing Perms Failed', f'The channel `{cls.channel_id}` does not exist.')
            return
        await channel.set_permissions(role, view_channel=True)
        await channel.set_permissions(cleanup, view_channel=False)
        await role.edit(mentionable=True, position=3)
        await cleanup.edit(position=2)

    async def _get_or_create_cleanup(self) -> discord.Role:
        """
        Fetches the 'Cleanup' role from the guild.
        If not found, a new role with the name 'Cleanup' is created and returned.
        """
        for role in self.bot.guild.roles:
            if role.name == 'Cleanup':
                return role
        return await self.bot.guild.create_role(name='Cleanup')

    async def _send_welcome(self, cls: ClassChannel, just_created: bool, user: discord.User):
        """
        Sends the 'welcome' message to the designated class channel.

        NOTE: It is imperative that you do this before sending an update to the repository
        as the ClassChannel's post_message_id is updated in this method, but not committed.
        """
        # double-check we have a current semester and the channel to send the embed to exists
        if not (semester := await self.repo.get_current_semester()):
            await self._send_failure(cls, 'Welcome Message Failed', 'There is no current semester in session.')
            return
        if not (channel := self.bot.guild.get_channel(cls.channel_id)):
            await self._send_failure(cls, 'Welcome Message Failed', f'The channel `{cls.channel_id}` does not exist.')
            return

        # prepare our embed
        embed = discord.Embed(title=f'📔 {cls.full_title}', color=Colors.Purple)
        embed.description = f'Welcome back, students!\n\n' \
                            f'Click the {WELCOME_MESSAGE_REACTION} reaction below to join the class.'
        embed.add_field(name='Semester', value=semester.semester_name)
        embed.add_field(name='Instructor', value=cls.class_professor)
        embed.add_field(name='Created By' if just_created else 'Unarchived By', value=user.mention)

        # send the embed + update the post_message_id in the ClassChannel model
        message = await channel.send(embed=embed)
        cls.post_message_id = message.id
        self.messages.add(message.id)

        # add the reaction for users to add/remove the class role
        await message.add_reaction(WELCOME_MESSAGE_REACTION)

    async def _send_failure(self, cls: ClassChannel, title: str, desc: str) -> None:
        """
        Sends an error embed to the notifications channel with the given title and description.
        This embed is sent to get a quick peek at the values stored in the ClassChannel upon error.
        """
        log.warning(f'Class service failure - {title}: {desc}')
        notif_channel = self._get_notifs_channel()
        embed = discord.Embed(title=f'📔 {title}', color=Colors.Error)
        embed.description = f'{desc}\nHere is what is currently stored in the database.'
        embed.add_field(name='Semester', value=cls.semester_id)
        embed.add_field(name='Channel Name', value=cls.channel_name)
        embed.add_field(name='Archived', value=bool(cls.class_archived))
        embed.add_field(name='Channel ID', value=cls.channel_id)
        embed.add_field(name='Role ID', value=cls.class_role_id)
        embed.add_field(name='Category ID', value=cls.category_id)
        await notif_channel.send(embed=embed)

    async def _check_semester(self, inter: discord.Interaction) -> ClassSemester | None:
        """
        This method double-checks that there is a current semester at runtime and returns it.
        If a current semester doesn't exist, an error embed is sent via the given interaction.

        `discord.Interaction.defer()` should NOT be called before calling this method.
        """
        # we should technically never enter this if statement at this point, but just in case...
        if not (semester := await self.repo.get_current_semester()):
            embed = error_embed(inter.user, 'No current semester in session.')
            await inter.response.send_message(embed=embed, ephemeral=True)
            return None
        return semester

    def _get_notifs_channel(self) -> discord.TextChannel:
        """
        Gets the notification channel for the guild.
        :raise AssertionError: Raised if `class_notifs_channel_id` is not set in the bot secrets.
        """
        channel = self.bot.guild.get_channel(bot_secrets.secrets.class_notifs_channel_id)
        assert channel is not None
        return channel

    async def load_service(self):
        if not (semester := await self.repo.get_current_semester()):
            return
        # since we have a current semester, schedule the archival for the end date...
        self.bot.scheduler.schedule_at(
            self.on_semester_archive(semester),
            time=semester.end_date
        )
        # cache our post message IDs and clean up any data since the last run
        for class_channel in await self.repo.get_semester_classes(semester):
            if not (channel := self.bot.guild.get_channel(class_channel.channel_id)):
                await self.repo.delete_class(class_channel)
                continue
            # post_message_id is not required to exist, so skip the classes that don't have it
            if not class_channel.post_message_id:
                continue
            # if the message from post_message_id no longer exists, set it to None and update our repo
            if not await fetch_optional_message(channel, class_channel.post_message_id):
                class_channel.post_message_id = None
                await self.repo.update_class(class_channel)
                continue
            # add the post message id to our messages to listen for
            self.messages.add(class_channel.post_message_id)
