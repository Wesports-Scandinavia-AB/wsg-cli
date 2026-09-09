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
    "MailboxSettings.Read",
    # Directory
    "User.Read.All",
    "Group.Read.All",
    "GroupMember.ReadWrite.All",
    "Directory.Read.All",
    "Device.Read.All",
    "AuditLog.Read.All",
    "ServiceHealth.Read.All",
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
