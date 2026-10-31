<#
open_windows.ps1 - open the Dendra application of a miner that runs in WSL 2, in the Windows browser.

The application (deploy/app/dendra_app.py) runs inside the distribution and listens on 127.0.0.1 there.
WSL 2 forwards a loopback port of its VM to the loopback of Windows (wslrelay listens on 127.0.0.1 only),
so the Windows browser reaches it at http://127.0.0.1:<port>/ and no other machine does. The port is
fixed, so that this script knows it before the application starts: -Port, else the DENDRA_APP_PORT
variable, else $DefaultPort below.

How the browser gets in, with the token in no URL, no command line and no terminal:
  1. this script draws a one-time token (32 random bytes) and writes an access page that posts it to the
     application, in the Dendra folder of %LOCALAPPDATA%. The folder and the page carry an access list
     with this account alone and no inherited entry, read back after it is set;
  2. it starts the application in the distribution (dendra_app.py --windows) with the token on its
     standard input and the WSL path of the access page, which the application removes at first use;
  3. before the browser is opened, the program on the port must prove it holds the token: this script
     sends a fresh nonce to /handoff/proof and checks the answer against HMAC-SHA256(token, label + nonce).
     Another program that took the port first cannot answer, and the page is then removed unopened;
  4. it opens the access page with the browser that handles http links. The application accepts the
     token once and answers with the session cookie (HttpOnly: no script reads it) and a script that keeps the
     other half of the session in that tab's sessionStorage, which only a page of this port can read.
The standard input of the application stays open while this script runs. When this script ends (Ctrl+C, the
window closed, the process killed), the application sees its input end, or a hang-up when wsl.exe itself is
killed with the window, and stops; it removes the page if it is still there.

Usage, from the folder that holds this file:
  powershell -ExecutionPolicy Bypass -File open_windows.ps1
  ... -Distro Ubuntu     the distribution that runs the miner (the one deploy/install.ps1 sets up)
  ... -Port 47860        the fixed port (or DENDRA_APP_PORT)
  ... -AppPath PATH      dendra_app.py inside the distribution, absolute or relative to the home
                         directory there (by default, in the clone deploy/install.ps1 makes)
  ... -NoBrowser         print the path of the access page instead of opening it

Exit codes: 0 the application stopped normally; 2 refused before anything started (no page is left
behind); 3 the application did not answer on the port; 4 a program that does not hold the token answers
on the port (the page is removed, the application stopped); otherwise the application's own exit code.
#>
[CmdletBinding()]
param(
  [string]$Distro = "Ubuntu",
  [int]$Port = 0,
  [string]$AppPath = "dendra-network/deploy/app/dendra_app.py",
  [switch]$NoBrowser
)

$ErrorActionPreference = "Continue"
$DefaultPort = 47860
# The bounds dendra_app.py applies to --windows (dendra_app.py::WINDOWS_PORT_MIN, WINDOWS_PORT_MAX).
$PortMin = 1024; $PortMax = 65535
# The label dendra_app.py puts before the nonce (dendra_app.py::HANDOFF_PROOF_LABEL).
$ProofLabel = "dendra-handoff-proof:"
$ReadySeconds = 30
$env:WSL_UTF8 = "1"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$Wsl = Join-Path (Join-Path $env:SystemRoot "System32") "wsl.exe"
$Sep = [string][char]92
$script:Page = ""

function Remove-Page {
  if ($script:Page -and (Test-Path -LiteralPath $script:Page)) { Remove-Item -LiteralPath $script:Page -Force -ErrorAction SilentlyContinue }
}
function Refuse([string]$m) {
  Remove-Page
  Write-Host "  [refused] $m Nothing was started."
  exit 2
}

# --- what is asked, checked before anything is written ---
if ($Port -eq 0) {
  if ($env:DENDRA_APP_PORT) {
    if ($env:DENDRA_APP_PORT -notmatch '^[0-9]{1,5}$') { Refuse "DENDRA_APP_PORT is not a port number." }
    $Port = [int]$env:DENDRA_APP_PORT
  } else {
    $Port = $DefaultPort
  }
}
if ($Port -lt $PortMin -or $Port -gt $PortMax) { Refuse "the port must be from $PortMin to $PortMax." }
if ($Distro -cnotmatch '^[A-Za-z0-9._-]{1,64}$') { Refuse "-Distro takes the name of a WSL distribution (letters, digits, dot, dash, underscore)." }
# An argument of wsl.exe that holds a space is passed between double quotes: a value that holds one is refused.
if (-not $AppPath -or $AppPath.Contains('"')) { Refuse "-AppPath is empty or holds a double quote." }
if (-not (Test-Path -LiteralPath $Wsl)) { Refuse "WSL is not installed on this PC ($Wsl is missing): run deploy/install.ps1 first." }

# A port already held is refused early, for a clear message. This check alone does not protect the token:
# the port can be taken between here and the start of the application, which the proof below catches.
try {
  $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
} catch {
  Refuse "the listening ports of this PC cannot be read ($($_.Exception.Message)), so port $Port cannot be known to be free."
}
if (@($listeners | Where-Object { $_.Port -eq $Port }).Count -gt 0) {
  Refuse "port $Port is already in use on this PC (another Dendra, or another program): close it, or choose another port with -Port."
}

# --- the access folder: this account alone ---
$Me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User

function Get-AccessList([string]$path, [bool]$folder) {
  # The access section alone: Set-Acl on what Get-Acl returns writes the audit section too, which needs a
  # privilege a user does not hold once the folder already carries this list.
  $section = [System.Security.AccessControl.AccessControlSections]::Access
  if ($folder) { return [System.IO.Directory]::GetAccessControl($path, $section) }
  return [System.IO.File]::GetAccessControl($path, $section)
}

function Set-OwnerOnly([string]$path, [bool]$folder) {
  $acl = Get-AccessList $path $folder
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($r in @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))) { [void]$acl.RemoveAccessRuleSpecific($r) }
  if ($folder) {
    $inherit = [System.Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
  } else {
    $inherit = [System.Security.AccessControl.InheritanceFlags]::None
  }
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($Me, [System.Security.AccessControl.FileSystemRights]::FullControl,
    $inherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)
  $acl.AddAccessRule($rule)
  if ($folder) { [System.IO.Directory]::SetAccessControl($path, $acl) } else { [System.IO.File]::SetAccessControl($path, $acl) }
}

function Test-OwnerOnly([string]$path, [bool]$folder) {
  # Read back: protected from inheritance, and one entry, which allows this account.
  $acl = Get-AccessList $path $folder
  $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
  return ($acl.AreAccessRulesProtected -and $rules.Count -eq 1 -and $rules[0].IdentityReference.Value -eq $Me.Value -and
          $rules[0].AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow)
}

if (-not $env:LOCALAPPDATA) { Refuse "LOCALAPPDATA is not set for this account." }
$Dir = Join-Path $env:LOCALAPPDATA "Dendra"
try {
  if (Test-Path -LiteralPath $Dir) {
    $item = Get-Item -LiteralPath $Dir -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) { Refuse "$Dir exists and is not a folder." }
    # A link would send the page somewhere else.
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { Refuse "$Dir is a link, not a folder." }
  } else {
    New-Item -ItemType Directory -Path $Dir -ErrorAction Stop | Out-Null
  }
  Set-OwnerOnly $Dir $true
} catch {
  Refuse "the folder $Dir cannot be made readable by this account only ($($_.Exception.Message))."
}
if (-not (Test-OwnerOnly $Dir $true)) { Refuse "the folder $Dir does not read back as readable by this account only." }
# An access page left by an earlier run holds a token that is spent or whose application has stopped.
Get-ChildItem -LiteralPath $Dir -Filter "open-*.html" -File -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

# --- the one-time token and its access page ---
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$Token = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
$script:Page = Join-Path $Dir ("open-" + [guid]::NewGuid().ToString("N") + ".html")
# The token travels in the body of a form post, never in a URL that the browser's history keeps.
$html = '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Dendra</title>' +
  '<form id="f" method="post" action="http://127.0.0.1:' + $Port + '/handoff">' +
  '<input type="hidden" name="t" value="' + $Token + '">' +
  '<p>Dendra opens in this tab. If it does not, press <button type="submit">Open Dendra</button>.</p></form>' +
  '<script>document.getElementById("f").submit()</script>'
try {
  [System.IO.File]::WriteAllText($script:Page, $html, (New-Object System.Text.UTF8Encoding($false)))
  Set-OwnerOnly $script:Page $false
} catch {
  Refuse "the access page cannot be written in $Dir ($($_.Exception.Message))."
}
if (-not (Test-OwnerOnly $script:Page $false)) { Refuse "the access page does not read back as readable by this account only." }

# The application removes the page through its WSL path when /handoff spends the token.
$WslPage = @(& $Wsl -d $Distro --exec wslpath -u $script:Page 2>$null) | Select-Object -First 1
if ($LASTEXITCODE -ne 0 -or -not $WslPage -or -not $WslPage.StartsWith("/")) {
  Refuse "WSL ($Distro) did not give the path of the access page (wslpath exit code $LASTEXITCODE): is the distribution installed?"
}
$WslPage = $WslPage.Trim()
# A PowerShell started from an app installed as a package writes %LOCALAPPDATA% into that package's own store, which
# WSL does not see: the page is looked for from the distribution before the application starts.
& $Wsl -d $Distro --exec test -f $WslPage 2>$null
$seen = $LASTEXITCODE
if ($seen -eq 1) {
  Refuse "WSL does not see the access page at ${WslPage}: this PowerShell runs inside a packaged app whose writes to %LOCALAPPDATA% go to its own store. Run this script from a PowerShell window opened from the Start menu."
}
if ($seen -ne 0) { Refuse "WSL ($Distro) could not look for the access page (exit code $seen)." }

# --- the application, with the token on its standard input ---
$argv = @("-d", $Distro, "--cd", "~", "--exec", "python3", $AppPath, "--windows", "--port", "$Port", "--access-file", $WslPage)
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Wsl
# wsl.exe reads its own options unquoted ("-d" in quotes is taken for a command): only a value that holds a
# space is put between double quotes.
$psi.Arguments = ($argv | ForEach-Object { if ($_.Contains(" ")) { '"' + $_ + '"' } else { $_ } }) -join " "
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
try {
  $app = [System.Diagnostics.Process]::Start($psi)
} catch {
  Refuse "wsl.exe could not be started ($($_.Exception.Message))."
}
# The input is not closed: its end is the signal that stops the application (dendra_app.py::watch_input_end).
# A console set to UTF-8 puts a byte order mark before the token; the application reads the line without it.
$app.StandardInput.Write($Token + "`n")
$app.StandardInput.Flush()

function Get-Proof([string]$nonce) {
  $h = New-Object System.Security.Cryptography.HMACSHA256 (,[System.Text.Encoding]::UTF8.GetBytes($Token))
  try { return (($h.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($ProofLabel + $nonce)) | ForEach-Object { $_.ToString("x2") }) -join "") }
  finally { $h.Dispose() }
}

function Test-Dendra {
  # Three answers: "ready" (the program on the port proved it holds the token), "wait" (nothing answers yet),
  # "impostor" (something answers and does not prove it). No proxy is asked.
  $nb = New-Object byte[] 16
  $g = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $g.GetBytes($nb) } finally { $g.Dispose() }
  $nonce = ($nb | ForEach-Object { $_.ToString("x2") }) -join ""
  try {
    $q = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/handoff/proof")
    $q.Proxy = $null
    $q.Timeout = 2000
    $q.Method = "POST"
    $q.ContentType = "application/x-www-form-urlencoded"
    $body = [System.Text.Encoding]::ASCII.GetBytes("n=" + $nonce)
    $q.ContentLength = $body.Length
    $s = $q.GetRequestStream(); try { $s.Write($body, 0, $body.Length) } finally { $s.Close() }
    $resp = $q.GetResponse()
  } catch [System.Net.WebException] {
    # A refused or reset connection: nothing listens yet. An HTTP error status: something listens and refuses.
    if ($_.Exception.Response) { return "impostor" }
    return "wait"
  } catch {
    return "wait"
  }
  try {
    $r = New-Object System.IO.StreamReader($resp.GetResponseStream())
    $text = $r.ReadToEnd()
  } finally { $resp.Close() }
  try { $got = "$((ConvertFrom-Json $text).proof)" } catch { return "impostor" }
  if ([int]$resp.StatusCode -eq 200 -and $got -ceq (Get-Proof $nonce)) { return "ready" }
  return "impostor"
}

$deadline = (Get-Date).AddSeconds($ReadySeconds)
$state = "wait"
while (-not $app.HasExited -and (Get-Date) -lt $deadline) {
  $state = Test-Dendra
  if ($state -ne "wait") { break }
  Start-Sleep -Milliseconds 300
}
$Token = $null
if ($state -eq "impostor") {
  Remove-Page
  Write-Host "  [!] A program that does not hold the token answers on http://127.0.0.1:${Port}/: the access page is removed unopened and Dendra is stopped. Close that program, or choose another port with -Port."
  try { $app.Kill() } catch { }
  exit 4
}
if ($state -ne "ready") {
  Remove-Page
  if ($app.HasExited) {
    $rc = $app.ExitCode
    Write-Host "  [!] The application stopped before it answered (exit code $rc); its message is above."
    if ($rc -eq 0) { exit 3 }
    exit $rc
  }
  Write-Host "  [!] The application did not answer on http://127.0.0.1:${Port}/ within $ReadySeconds seconds: it is stopped."
  try { $app.Kill() } catch { }
  exit 3
}

function Get-HttpBrowser {
  # The command of the program that opens http links for this account. A .html file may open in an editor;
  # an http link opens in a browser.
  $key = @("Software", "Microsoft", "Windows", "Shell", "Associations", "UrlAssociations", "http", "UserChoice") -join $Sep
  $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($key)
  if (-not $k) { return $null }
  try { $prog = "$($k.GetValue('ProgId'))" } finally { $k.Close() }
  if (-not $prog) { return $null }
  $c = [Microsoft.Win32.Registry]::ClassesRoot.OpenSubKey((@($prog, "shell", "open", "command") -join $Sep))
  if (-not $c) { return $null }
  try { $cmd = "$($c.GetValue(''))" } finally { $c.Close() }
  if ($cmd -notmatch '^"([^"]+)"(.*)$') { return $null }
  $exe = $Matches[1]; $rest = $Matches[2]
  if (-not $rest.Contains("%1") -or -not (Test-Path -LiteralPath $exe)) { return $null }
  return @($exe, $rest)
}

if ($NoBrowser) {
  Write-Host "Dendra is running. Open this page in a browser of this account: $($script:Page) (readable by you only, removed after first use)."
} else {
  $b = Get-HttpBrowser
  if ($b) {
    try {
      Start-Process -FilePath $b[0] -ArgumentList ($b[1].Replace("%1", '"' + $script:Page + '"')).Trim() -ErrorAction Stop
    } catch {
      Write-Host "No browser could be started ($($_.Exception.Message)). Open this page in a browser of this account: $($script:Page)"
    }
  } else {
    Write-Host "The browser for http links could not be read. Open this page in a browser of this account: $($script:Page)"
  }
}

# Best effort only: a closed window can end this script before finally runs. The application removes the
# page itself when it stops (dendra_app.py::watch_input_end, dendra_app.py::stop_on_hangup).
try { $app.WaitForExit() } finally { Remove-Page }
exit $app.ExitCode
