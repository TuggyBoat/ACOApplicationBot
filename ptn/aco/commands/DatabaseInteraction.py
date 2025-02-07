import traceback
from datetime import datetime, timedelta
import os.path

import discord
from dateutil import parser
from discord import NotFound, HTTPException, app_commands
from discord.ext import tasks, commands
from oauth2client.service_account import ServiceAccountCredentials
import gspread
from discord.ext.commands import Cog

from ptn.aco import constants
from ptn.aco.UserData import UserData
from ptn.aco.constants import bot_guild_id, server_admin_role_id, server_mod_role_id, get_bot_notification_channel, \
    get_server_aco_role_id, get_member_role_id
from ptn.aco.database.database import affiliator_db, affiliator_conn, dump_database, affiliator_lock
from ptn.aco.modules.Helper import check_roles
from ptn.aco.bot import bot


class InvalidUser(Exception):
    pass


class DatabaseInteraction(Cog):

    @commands.Cog.listener()
    async def on_ready(self):
        print('Starting the polling task')
        await self.timed_scan.start()

    @tasks.loop(hours=24)
    async def timed_scan(self):
        print(f'Automatic database polling started at {datetime.now()}')
        self.running_scan = True
        result = await self._update_db()
        self.running_scan = False
        print('Automatic database scan completed, next scan in 24 hours')

        if result['added_count'] == 0:
            # Nothing happened, lets ping the channel just to show we are still running.
            notification_channel = bot.get_channel(get_bot_notification_channel())

            # Ok no updates were requested, just drop a message saying it was triggered.
            message = await notification_channel.send(
                "No new ACO applications were detected today. Next scan in 24 hours."
            )
            await message.add_reaction('👁️')

    @timed_scan.after_loop
    async def timed_scan_after_loop(self):
        self.running_scan = False
        if self.timed_scan.failed():
            print("timed_scan after_loop().Task has failed.")

    @timed_scan.error
    async def timed_scan_error(self, error):
        self.running_scan = False
        if not self.timed_scan.is_running() or self.timed_scan.failed():
            print("timed_scan error(). task has failed.")
        print(error)
        traceback.print_exc()

    def __init__(self):
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

        if not os.path.join(os.path.expanduser('~'), '.ptnuserdata.json'):
            raise EnvironmentError('Cannot find the user data json file.')

        credentials = ServiceAccountCredentials.from_json_keyfile_name(
            os.path.join(os.path.expanduser('~'), '.ptnuserdata.json'), scope)

        # authorize the client sheet
        self.client = gspread.authorize(credentials)

        affiliator_db.execute(
            "SELECT * FROM trackingforms"
        )
        forms = dict(affiliator_db.fetchone())

        self.worksheet_key = forms['worksheet_key']

        # On which sheet is the actual data.
        self.worksheet_with_data_id = forms['worksheet_with_data_id']

        print(f'Building worksheet with the key: {self.worksheet_key}')
        self.running_scan = False
        try:
            workbook = self.client.open_by_key(self.worksheet_key)
            print(workbook)
            print(len(workbook.worksheets()))
            self.tracking_sheet = workbook.get_worksheet(self.worksheet_with_data_id)
        except gspread.exceptions.APIError as e:
            print(f'Error reading the worksheet: {e}')

    """ Unused?"""
    # TODO: Flesh out into proper command
    @app_commands.command(name='find_user')
    @check_roles(constants.any_elevated_role)
    async def find_user_test(self, interaction: discord.Interaction, member: str):
        print(f'Looking for: {member}')
        bot_guild = bot.get_guild(bot_guild_id())
        dc_user = bot_guild.get_member_named(member)
        print(f'Result: {dc_user}')
        return await interaction.user.send_message(f'User {dc_user.name} has roles: {dc_user.roles}')

    @app_commands.command(name="scan_aco_applications", description="Populates the ACO database from the updated google "
                                                                    "sheet. Admin/Mod role required.")
    @check_roles(constants.any_elevated_role)
    async def user_update_database_from_googlesheets(self, interaction: discord.Interaction):
        """
        Slash command for updating the database from the GoogleSheet.

        :returns: A discord embed to the user.
        :rtype: None
        """
        print(f'User {interaction.user} requested to re-populate the database at {datetime.now()}')
        if self.running_scan:
            return await interaction.response.send_message('DB scan is already in progress.')

        try:
            result = await self._update_db()
            msg = 'Check the ACO application channel for new applications' if result['added_count'] > 0 \
                else 'No new applications found'
            embed = discord.Embed(title="ACO DB Update ran successfully.")
            embed.add_field(name='Scan completed', value=msg, inline=False)

            return await interaction.response.send_message(embed=embed)

        except ValueError as ex:
            return await interaction.response.send_message(str(ex))

    async def _update_db(self):
        """
        Private method to wrap the DB update commands.

        :returns:
        :rtype:
        """
        if not self.tracking_sheet:
            raise EnvironmentError('Sorry this cannot be ran as we have no form for tracking ACOs presently. '
                                   'Please set a new form first.')

        updated_db = False
        added_count = 0
        embed_list = []  #: [discord.Embed]

        # A JSON form tracking all the records
        records_data = self.tracking_sheet.get_all_records()

        total_users = len(records_data)
        print(f'Updating the database we have: {total_users} records in the tracking form.')
        bot_guild = bot.get_guild(bot_guild_id())

        # First row is the headers, drop them.
        for record in records_data:
            print(record)
            # Iterate over the records and populate the database as required.

            # Check if it is in the database already by checking timestamp and carrier ID. This allows multiple
            # applications

            affiliator_db.execute(
                "SELECT * FROM acoapplications WHERE fleet_carrier_id LIKE (?) AND timestamp = (?)",
                (f'%{record["Carrier ID"].upper()}%', f'{record["Timestamp"]}')
            )
            userdata = [UserData(user) for user in affiliator_db.fetchall()]
            if len(userdata) > 1:
                raise ValueError(f'{len(userdata)} users are listed with this carrier ID:'
                                 f' {record["Carrier ID"].upper()}. Problem in the DB!')

            if userdata:
                # We have a user object, just check the values and update it if needed.
                print(f'The user for {record["Carrier ID"].upper()} exists, no notification required')

            else:
                added_count += 1
                user = UserData(record)
                print(user.to_dictionary())
                print(f'Application for "{record["Carrier Name"]}" is not yet in the database - adding it')

                # TODO: We could track in DB the member role when it is added, as a reference point and use that
                #  here? Probably overkill?
                try:
                    affiliator_lock.acquire()
                    affiliator_db.execute(''' 
                    INSERT INTO acoapplications VALUES(NULL, ?, ?, ?, ?, ?, ?, ?, ?) 
                    ''', (
                        user.discord_username, user.ptn_nickname, user.cmdr_name,
                        user.fleet_carrier_name, user.fleet_carrier_id, user.ack,
                        user.user_claims_member, user.timestamp
                        )
                    )
                finally:
                    affiliator_lock.release()

                reason = ""
                eligible_for_aco = 'Unknown'

                try:
                    dc_user = bot_guild.get_member_named(user.discord_username)
                    if not dc_user:
                        print(f'Invalid user for: {user.discord_username}')
                        raise InvalidUser(f'Invalid user for: {user.discord_username}')

                    print(f'USER: {dir(dc_user)}')
                    print(type(dc_user))
                    member_role = discord.utils.get(bot_guild.roles, id=get_member_role_id())
                    print(member_role)
                    member = member_role in dc_user.roles

                    if member:
                        print(f'User {dc_user} has the member role.')
                        # We have the role, go check member since when
                        try:
                            affiliator_db.execute(
                                "SELECT * FROM membertracking WHERE discord_username LIKE (?)", (f'%{dc_user}%',)
                            )
                            member_tracking_since = dict(affiliator_db.fetchone())
                            print(f'User data: {user}')
                            now = datetime.now()
                            role_since = parser.parse(member_tracking_since['date'])

                            time_with_role = now - role_since
                            if time_with_role.days >= 14:
                                eligible_for_aco = True
                            else:
                                eligible_from = role_since + timedelta(days=14)
                                eligible_for_aco = False
                                reason = f'**Reason:** User member for: {time_with_role.days} days.\n' \
                                         f'**Eligible from**: {eligible_from.strftime("%Y-%m-%d %H:%M:%S")}.\n'
                        except TypeError as ex:
                            reason = f'**Reason:** User not found in Database.\n'
                            print('Error when converting the membertracking object to a dict - is the user present?')
                            print(ex)
                    else:
                        eligible_for_aco = False
                        print(f'User {dc_user} has no member role: {dc_user.roles}')
                        reason = '**Reason:** No member role found.\n'
                except (InvalidUser, NotFound, HTTPException) as ex:
                    print(f'Unable to member status for user {user.discord_username}: {ex}')
                    member = 'Unknown.'
                    reason = 'Unable to determine membership\n'

                # Allow flagging of multiple attempts to join
                affiliator_db.execute(
                    "SELECT * FROM acoapplications WHERE fleet_carrier_id LIKE (?)",
                    (f'%{record["Carrier ID"].upper()}%',)
                )
                # We already stuck it in the DB, so the counter is this current value.
                application_attempt = len([UserData(user) for user in affiliator_db.fetchall()])

                embed = discord.Embed(
                    title='New ACO application detected.',
                    description=f'**User:** {user.ptn_nickname}\n'
                                f'**Discord Username:** {user.discord_username}\n'
                                f'**Cmdr Name:** {user.cmdr_name}\n'
                                f'**Fleet Carrier:** {user.fleet_carrier_name} ({user.fleet_carrier_id})\n'
                                f'**Has member role:** {member}.\n'
                                f'**Eligible for ACO:** {eligible_for_aco}\n'
                                f'{reason}'
                                f'**Applied At:** {user.timestamp}\n'
                                f'**Application Attempt:** {application_attempt}'
                )
                embed.set_footer(text='Please validate membership and vote on this proposal')
                embed_list.append(embed)
                updated_db = True
                print('Added ACO application to the database')

        if updated_db:
            # Write the database and then dump the updated SQL
            try:
                affiliator_lock.acquire()
                affiliator_conn.commit()
            finally:
                affiliator_lock.release()
            dump_database()
            print('Wrote the database and dumped the SQL')

            # Send all the notifications now
            notification_channel = bot.get_channel(get_bot_notification_channel())

            await notification_channel.send(f'Priority transmission {len(embed_list)} application'
                                            f'{"s" if len(embed_list) > 1 else ""} incoming.')

            for entry in embed_list:
                message = await notification_channel.send(embed=entry)
                await message.add_reaction('👍')
                await message.add_reaction('👎')

        return {
            'updated_db': updated_db,
            'added_count': added_count,
        }

    @app_commands.command(name='grant_affiliate_status', description='Toggle user\'s ACO role. Admin/Mod role required.')
    async def toggle_aco_role(self, interaction: discord.Interaction, user: discord.Member):
        print(f"toggle_aco_role called by {interaction.user} in {interaction.channel} for {user}")
        # set the target role
        print(f"ACO role ID is {get_server_aco_role_id()}")
        role = discord.utils.get(interaction.guild.roles, id=get_server_aco_role_id())
        print(f"ACO role name is {role.name}")

        if role in user.roles:
            # toggle off
            print(f"{user} is already an ACO, removing the role.")
            try:
                await user.remove_roles(role)
                response = f"{user.display_name} no longer has the ACO role."
                return await interaction.response.send_message(content=response)
            except Exception as e:
                print(e)
                await interaction.response.send_message(f"Failed removing role from {user}: {e}")
        else:
            # toggle on
            print(f"{user} is not an ACO, adding the role.")
            try:
                await user.add_roles(role)
                print(f"Added ACO role to {user}")
                response = f"{user.display_name} now has the ACO role."
                return await interaction.response.send_message(content=response)
            except Exception as e:
                print(e)
                await interaction.response.send_message(f"Failed adding role to {user}: {e}")
