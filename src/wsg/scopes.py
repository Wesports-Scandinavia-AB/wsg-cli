"""
Default delegated Graph scopes requested at login.

One login should cover every script we run, so this is the union of what they
need. Add a scope here when a script starts needing it, then run `wsg graph login`
again. Users without the matching admin role can still sign in with a narrower
set: `wsg graph login --scopes User.Read,Files.ReadWrite,Mail.Send`.
"""

DEFAULT_SCOPES = [
    # Own account
    "User.Read",
    "Files.ReadWrite",
    "Mail.Send",
    # Mail.ReadWrite behövs för createReply/drafts, dvs. svara i tråd
    "Mail.ReadWrite",
    # Läsa delade brevlådor jag har FullAccess till (person@example.com, person@example.com,
    # person@example.com) - mail-triage tittar i dem varje varv. Scopet ger
    # ingen åtkomst i sig; Exchange-behörigheten på brevlådan avgör fortfarande.
    "Mail.Read.Shared",
    "MailboxSettings.Read",
    # Directory
    "User.Read.All",
    # User.ReadWrite.All krävs för lösenordsreset (wsg user password) och för
    # revokeSignInSessions. Scopet räcker inte i sig: kontot måste dessutom ha
    # en roll som får återställa lösenord, t.ex. Helpdesk-administratör.
    "User.ReadWrite.All",
    "Group.Read.All",
    "GroupMember.ReadWrite.All",
    "Directory.Read.All",
    # Läser om password writeback är på, dvs. om ett synkat konto alls kan
    # lösenordsbytas i Entra. Utan scopet hoppar wsg user password över kollen.
    "OnPremDirectorySynchronization.Read.All",
    # Claims på en enterprise-apps SAML-token (servicePrincipals/{id}/claimsPolicy),
    # t.ex. vad AARO SAML får som NameID. Skriver bara appkonfiguration, men kräver
    # också en roll som Cloud Application Administrator för att gå igenom.
    "Policy.ReadWrite.ApplicationConfiguration",
    "Device.Read.All",
    "AuditLog.Read.All",
    "ServiceHealth.Read.All",
    # Planner. Tasks.Read täcker både /me/planner/plans och en enskild board;
    # namnen på tilldelade och på ägargruppen kommer från User.Read.All och
    # Group.Read.All ovan, inte härifrån.
    "Tasks.Read",
    # Teams-chatt: skriva till en kollega i 1:1. Chat.Create hittar eller skapar
    # chatten (POST /chats returnerar den befintliga), ChatMessage.Send postar i den.
    # Inget av dem läser chattar.
    "Chat.Create",
    "ChatMessage.Send",
    # Intune
    "DeviceManagementManagedDevices.Read.All",
    "DeviceManagementManagedDevices.ReadWrite.All",
    "DeviceManagementManagedDevices.PrivilegedOperations.All",
    "DeviceManagementConfiguration.Read.All",
    "DeviceManagementServiceConfig.Read.All",
    "DeviceManagementServiceConfig.ReadWrite.All",
    "DeviceManagementScripts.ReadWrite.All",
    "DeviceManagementApps.ReadWrite.All",
    # LAPS-lösenord för enskild enhet
    "DeviceLocalCredential.Read.All",
]
