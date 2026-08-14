param(
  [Parameter(Mandatory=$true)][string]$TemplatePath,
  [Parameter(Mandatory=$true)][string]$OutputPath,
  [Parameter(Mandatory=$true)][string]$DaemonExecutable,
  [Parameter(Mandatory=$true)][string]$DaemonArguments,
  [Parameter(Mandatory=$true)][string]$InstallRoot,
  [Parameter(Mandatory=$true)][string]$DataRoot,
  [Parameter(Mandatory=$true)][string]$ServiceLogRoot,
  [Parameter(Mandatory=$true)][string]$ListenHost,
  [Parameter(Mandatory=$true)][ValidateRange(1,65535)][int]$ListenPort
)
$ErrorActionPreference = 'Stop'
$values = @{
  '@RK_DAEMON_EXECUTABLE@' = $DaemonExecutable
  '@RK_DAEMON_ARGUMENTS@' = $DaemonArguments
  '@RK_INSTALL_ROOT@' = $InstallRoot
  '@RK_DATA_ROOT@' = $DataRoot
  '@RK_SERVICE_LOG_ROOT@' = $ServiceLogRoot
  '@RK_LISTEN_HOST@' = $ListenHost
  '@RK_LISTEN_PORT@' = [string]$ListenPort
}
$xml = [IO.File]::ReadAllText($TemplatePath, [Text.Encoding]::UTF8)
foreach ($entry in $values.GetEnumerator()) {
  $escaped = [Security.SecurityElement]::Escape($entry.Value)
  $xml = $xml.Replace($entry.Key, $escaped)
}
if ($xml.Contains('@RK_')) { throw 'Unresolved RK service template token' }
[IO.File]::WriteAllText($OutputPath, $xml, [Text.UTF8Encoding]::new($false))
