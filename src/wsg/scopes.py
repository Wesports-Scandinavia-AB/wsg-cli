"""
Delegated Graph scopes requested by `wsg graph login`.

This is the base set: what anyone in the group can use on their own account, plus
read access to the directory for names. It is also exactly what the "WeSports CLI"
app is consented for, org-wide, so a scope added here does nothing until the app's
API permissions and its admin consent are widened to match; until then the login
names it under "Ej beviljade scopes".

Directory, device and policy scopes live with the admin commands in wsg-admin-cli,
which sign in through their own app. Before 2026-10-09 this list held both, and
the client was Microsoft's Graph CLI app, whose tokens carried every scope ever
consented for it regardless of this list.
"""

DEFAULT_SCOPES = [
    # Own account
    "User.Read",
    "Files.ReadWrite",
    "Mail.Send",
    # Mail.ReadWrite behövs för createReply/drafts, dvs. svara i tråd
    "Mail.ReadWrite",
    # Delade brevlådor man har FullAccess till. Scopet ger ingen åtkomst i sig;
    # Exchange-behörigheten på brevlådan avgör fortfarande.
    "Mail.Read.Shared",
    # ReadWrite för inkorgsregler (messageRules), inte bara tidszonen
    "MailboxSettings.ReadWrite",
    # wsg calendar: speglade bokningar som upptagen-tid i den egna kalendern
    "Calendars.ReadWrite",
    # Läsa kalendern på en delad brevlåda man har FullAccess till, t.ex.
    # coordination@wesportsgroup.com. Calendars.ReadWrite räcker bara för den
    # egna kalendern; Graph kräver .Shared för en annans, FullAccess eller ej.
    "Calendars.Read.Shared",
    # Namn på personer och grupper, t.ex. vilka som är tilldelade i Planner
    # (directoryObjects/getByIds). Bara läsning, samma katalog som Outlook visar.
    "User.Read.All",
    "Group.Read.All",
    # Planner. Tasks.Read täcker både /me/planner/plans och en enskild board.
    "Tasks.Read",
    # Teams-chatt: skriva till en kollega i 1:1. Chat.Create hittar eller skapar
    # chatten (POST /chats returnerar den befintliga), ChatMessage.Send postar i den.
    # Chat.Read läser chattar, inklusive inklistrade bilder (hostedContents) som
    # M365-connectorn inte visar.
    "Chat.Create",
    "ChatMessage.Send",
    "Chat.Read",
    # wsg teams find: /search/query över chattmeddelanden kräver ChannelMessage.Read.All
    # bredvid Chat.Read, även när alla träffar ligger i chattar (2026-10-12). Delegerat:
    # bara kanaler personen redan ser i Teams.
    "ChannelMessage.Read.All",
    # wsg teams read --files: a file shared in a chat lives in the sender's OneDrive,
    # and Files.ReadWrite reaches only your own. Files.Read.All is delegated, so it
    # opens what the person could already open in Teams and nothing else (2026-10-13).
    "Files.Read.All",
]
