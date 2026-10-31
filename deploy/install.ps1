<#
install.ps1 - prepare a Windows PC for a Dendra miner: WSL 2 + Ubuntu, then deploy/install.sh inside it.

What this file is for. On Windows the miner runs inside WSL 2, and getting there by hand means: install
WSL, install a distribution, create its user, enable systemd, restart WSL, install Docker and the NVIDIA
toolkit inside, clone the repository, and only then run join.sh. This file walks that path and stops at
the first step that needs you (a restart, the first launch of the distribution). It runs the documented
Microsoft commands and then hands over to deploy/install.sh, which prepares the Linux side and hands
over to join.sh in turn.

What it does, in this order, and nothing else:
  1. reads the host: Windows build, administrator rights, the account, memory, free disk on the drive
     that holds the distribution, WSL itself, the distribution and its WSL version, systemd, the NVIDIA
     driver;
  2. installs WSL and the distribution when missing (wsl --install -d <Distro> --no-launch); a restart
     is then required, and this file says so and stops. Before it stops it leaves a one-time logon task that runs
     this file again after the restart (see "Resuming after a restart" below); without the task, run it again;
  3. asks you to open the distribution once from the Start menu when its Linux user does not exist yet
     (that first launch is where the user and password are created), and stops. A run the logon task started
     waits for that user to exist instead, and goes on;
  4. enables systemd in the distribution (/etc/wsl.conf, [boot] systemd=true) and restarts WSL, because
     Docker inside WSL is a systemd service;
  5. installs git and ssh-keygen inside the distribution and clones the repository into ~/dendra-network there, with
     a marker in the clone's .git that tells install.sh the clone was made for this run (see "A clone made by
     install.ps1" in deploy/install.sh), and copies deploy/install.sh, with the probe and the pins next to it, into
     the clone's .git (dendra-installer): the copy of main for a first installation. A clone already there is
     fetched first; if its kit recorded docker/RELEASE_SIGNERS, the copy is the one of the newest release tag that
     file verifies (no older tag in its place), else the copy of main;
  6. runs that copy of install.sh with --yes inside the distribution (see that file for what it does: it decides
     the role of this PC with deploy/hw_probe.sh --role before it changes anything). The copy, not the clone's own
     deploy/install.sh: once the clone sits on an older release, that file is the older release's installer;
  7. registers a logon task that keeps the WSL virtual machine alive, unless install.sh refused this PC (exit 2):
     a PC with no role gets no hidden task keeping a VM up for nothing. WSL stops its VM when the last session
     closes; a miner inside it stops with it, and nothing on screen says so. -NoKeepAlive skips it;
  8. prints the command that reads this PC's health from Windows, and where its alert file lives: the hourly
     check runs inside the distribution, where nothing on the Windows desktop shows it.

It changes nothing without -Yes: run it once without the switch and it prints the plan.
It is not meant to be run from a URL. Download it, read it, then run it from an administrator PowerShell
opened from your own account (WSL, the clone and the logon task belong to the account that runs this):

  powershell -ExecutionPolicy Bypass -File .\install.ps1            # plan only
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Yes       # do it
  ... -Yes -Light        read the chain from the public RPC instead of running a node (join.sh --remote-rpc)
  ... -Yes -Miner        mine without asking for the judge role (needs an NVIDIA card WSL can use: no mining
                         model runs on the CPU; deploy/hw_probe.sh --role decides)      ... -Yes -Validator
  ... -Yes -NoJudge      decline the judge seat on a PC that can judge (the seat is on by default there; recorded)
  ... -Yes -NoAutoUpdate turn off the daily automatic update of the kit (on by default; DENDRA_AUTO_UPDATE=0)
  ... -Yes -PayoutLock   declare the payout address locked with its first declaration (cannot be corrected later)
  ... -NoKeepAlive       do not register the logon task              ... -Distro <name>   (default Ubuntu)
  ... -NoResume          do not leave the resume task before the restart (run this file again by hand)
  ... -Yes -PayoutAddress dendra1...   where the Final Testnet Season pays this miner (install.sh --payout-address,
                         then join.sh, which verifies its checksum before anything changes; a key not on this PC)
  ... -Yes -Gpus all     one miner identity per NVIDIA card (install.sh --gpus, then join.sh --gpus; also a
                         list of card indices, 0,2, or of card UUIDs). Not measured on real multi-card
                         hardware under WSL 2: there each card is held to its identity by CUDA_VISIBLE_DEVICES,
                         and the hourly self-test's pinning check (H6) says whether it held.

The disk floor checked here is the miner's. Whether this PC can judge is decided by the RAM of the WSL
VM, which cannot be read before WSL exists; deploy/install.sh asks deploy/hw_probe.sh inside the
distribution, before installing anything, and applies the judge's higher floor only to a host that can
judge.

Resuming after a restart. With -Yes, when WSL has just been installed, this file copies itself to a folder of
%ProgramData% that only the LocalSystem account and Administrators can change (read back after it is set) and registers the task
"Dendra install resume". At your next logon, 30 seconds after, the task runs that copy with the same switches and
-Resume, as you, with administrator rights and no prompt, in a visible window that stays open. The task runs with
those rights, which is why the copy is not left where a program without them could edit it. A resumed run disables
the task when it starts, so an installation you abandon does not come back at every logon, and the task deletes
itself 14 days after it was registered. A run started by hand with -Yes removes the task and the copy, and so does
-Uninstall -Yes. After `wsl --update` (WSL is shut down, not Windows) this file starts itself again once in the
same window. -Resume is for the task: do not use it by hand.

One run at a time. A run with -Yes takes a lock (a Windows mutex of your logon session) before it reads or changes
anything, and lets it go when it ends, also when it stops at a restart or at the first launch. A second run started
while the first still works, by hand while the window of the task is open or the other way round, refuses, says why,
and changes nothing: it removes neither the task nor the copy. A run without -Yes only prints a plan and takes no lock.

Uninstall, the same way: plan first, nothing without -Yes.
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall              # plan only
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall -Yes         # do it
  ... -Uninstall -Yes -DeleteKeys   also delete the miner's keys, after a verified backup in the same run
  ... -Uninstall -Yes -KeepRegistration   a miner still registered (or whose registration cannot be read):
                                          remove the kit anyway and keep its keys (uninstall.sh --keep-registration)
It runs deploy/uninstall.sh inside the distribution (see that file for what it removes and what it
keeps: the miner's keys always, unless -DeleteKeys), then removes the logon task. It never unregisters
the distribution: `wsl --unregister` deletes everything in it, Docker volumes and keys included, and is
printed for you to run yourself once the backup is copied out.

No inbound port is opened, and a miner needs none: it dials out to the relay, the RPC and the faucet.
A home connection behind carrier-grade NAT qualifies; no router page is involved.

Exit codes: 0 done, or a step that needs you (restart, first launch, re-run) and says so - 1 a step failed
            2 refused (not administrator, another account, Windows too old, WSL 1, disk or memory under the
              floor, changes needed without -Yes; and install.sh's or uninstall.sh's own refusals)
            3 not measurable
#>
[CmdletBinding()]
param(
  [switch]$Yes,
  [switch]$Light,
  [switch]$Miner,
  [switch]$NoJudge,
  [switch]$NoAutoUpdate,
  [switch]$PayoutLock,
  [switch]$Validator,
  [switch]$NoKeepAlive,
  [switch]$NoResume,
  [switch]$Resume,
  [switch]$Uninstall,
  [switch]$DeleteKeys,
  [switch]$KeepRegistration,
  [string]$Distro = "Ubuntu",
  [string]$ConfigUrl = "https://testnet-api.dendranetwork.com/network-info.txt",
  [string]$RepoUrl = "https://github.com/DendraNetwork/dendra-network.git",
  [string]$Gpus = "",
  [string]$PayoutAddress = ""
)

# The payout address: -PayoutAddress is relayed to install.sh --payout-address, which relays it to join.sh. It
# travels inside a command line built as a string, so it is checked here first, as install.sh checks it: letters
# and digits only, over the whole value (\z: a final line break does not end it, as $ would allow), never printed
# when refused; a validator is paid nothing by the season. join.sh verifies its checksum before anything changes.
if ($PayoutAddress -and ($PayoutAddress -cnotmatch '^[A-Za-z0-9]{1,128}\z')) {
  Write-Host "  [refused] -PayoutAddress holds characters no address has: it is not passed on (the value is not printed). Nothing was changed."
  exit 2
}
if ($PayoutAddress -and $Validator) {
  Write-Host "  [refused] -PayoutAddress applies to a miner, not to -Validator. Nothing was changed."
  exit 2
}

# One identity per card: -Gpus is relayed to install.sh --gpus, which relays it to join.sh. It travels inside a
# command line built as a string, so it is checked here first, case-sensitively, by the expression both of
# those files apply (GPUS_SPEC_RE); a validator is one node, never one per card.
if ($Gpus -and ($Gpus -cnotmatch '^(all|[0-9]+(,[0-9]+)*|GPU-[0-9a-f-]+(,GPU-[0-9a-f-]+)*)$')) {
  Write-Host "  [refused] -Gpus takes all, a list of card indices (0,2) or a list of card UUIDs (GPU-...). Nothing was changed."
  exit 2
}
if ($Gpus -and $Validator) {
  Write-Host "  [refused] -Gpus runs one miner identity per card; a validator is one node. Nothing was changed."
  exit 2
}

$ErrorActionPreference = "Continue"
# wsl.exe prints UTF-16 by default; with this variable the Store build prints UTF-8, which PowerShell
# 5.1 reads as text. The inbox build ignores it, which is one reason the inbox build is updated first.
$env:WSL_UTF8 = "1"
$Wsl = Join-Path $env:SystemRoot "System32\wsl.exe"
$TaskName = "Dendra WSL keep-alive"
# The miner floors of deploy/install.sh, read here on the Windows drive that holds the distribution's
# disk: inside the VM `df` sees a sparse virtual disk, not the space left on that drive. The judge's
# higher disk floor is not applied here: it only concerns a host that can judge, which is decided by the
# RAM of the WSL VM -- unreadable before WSL exists. deploy/install.sh decides it inside the
# distribution with deploy/hw_probe.sh, before it installs anything, on this same drive (the drive is
# passed to it below).
$MinDiskGB = 12; if ($env:DENDRA_MIN_DISK_GB) { $MinDiskGB = [int]$env:DENDRA_MIN_DISK_GB }
$MinRamMB = 4000; if ($env:DENDRA_MIN_RAM_MB) { $MinRamMB = [int]$env:DENDRA_MIN_RAM_MB }

function Say([string]$m) { Write-Host $m }
function Warn([string]$m) { Write-Host "  [!] $m" }
function Refuse([string]$m) { Write-Host "  [refused] $m" }
$script:WslRc = 0
function WslRun([string[]]$a) {
  # Runs wsl.exe and returns what it printed (stdout and stderr), whatever the exit code; the code is
  # left in $script:WslRc. The text is for humans and for sentinels: decisions below never rest on
  # "the output is non-empty", because wsl.exe writes its own warnings on stderr and PowerShell wraps
  # them in error records -- a probe is answered by a line it can match, or it is not answered.
  $out = & $Wsl @a 2>&1
  $script:WslRc = $LASTEXITCODE
  $lines = @()
  foreach ($o in $out) {
    if ($o -is [System.Management.Automation.ErrorRecord]) { $lines += $o.Exception.Message } else { $lines += [string]$o }
  }
  return (($lines -join "`n").Trim())
}
function WslSh([string]$user, [string]$script) {
  # Runs a shell script inside the distribution and returns its exit code. The script travels as one
  # base64 argument: nothing is piped, so the CR LF and the byte-order mark that PowerShell 5.1 appends
  # to a piped string never reach sh, and `--exec` keeps the login shell from re-reading the text. The
  # `tr -d` also strips a CR that a Windows checkout of this file could have put into the here-strings.
  $b = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($script))
  $cmd = "echo $b | base64 -d | tr -d '\r' | sh -s"
  if ($user) { & $Wsl -d $Distro -u $user --exec sh -c $cmd | Out-Host } else { & $Wsl -d $Distro --exec sh -c $cmd | Out-Host }
  return $LASTEXITCODE
}

# begin resume functions
# Resuming after a restart: the header of this file says what it does and why the copy of this file is kept where
# only administrators can change it.
$ResumeTaskName = "Dendra install resume"
$AdminSids = @("S-1-5-18", "S-1-5-32-544")
$ResumeFile = ""
# The folder comes from the system (the known folder of the common application data), never from the ProgramData
# variable: a variable is set per account and per process, and the task runs with administrator rights from this copy.
$ResumeBase = [Environment]::GetFolderPath("CommonApplicationData")
if ($ResumeBase) { $ResumeFile = Join-Path (Join-Path $ResumeBase "Dendra") "install.ps1" }
$SelfRunner = Join-Path $PSHOME "powershell.exe"
$script:ResumeWhy = ""
$script:DistroProbeText = ""

function Get-DistroUserState {
  # A distribution is usable once its first launch created a regular user (uid 1000). Before that,
  # `wsl -d <Distro>` starts the setup dialog, which this file must not drive blindly. The answer is
  # a sentinel on stdout -- `uid1000=<name>` or `uid1000=` -- never the mere presence of output.
  # Returns "ready", "uninitialized" or "unreachable"; what wsl.exe printed is left in $script:DistroProbeText.
  $u = WslRun @("-d", $Distro, "-u", "root", "--exec", "sh", "-c", 'echo uid1000=$(getent passwd 1000 | cut -d: -f1)')
  $script:DistroProbeText = $u
  if ($u -match '(?m)^uid1000=(\S+)\s*$') { return "ready" }
  if ($u -match '(?m)^uid1000=\s*$') { return "uninitialized" }
  return "unreachable"
}

function Wait-DistroAccount([int]$seconds, [int]$every) {
  # True once the first launch created the regular user, False when $seconds went by without it.
  $until = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $until) {
    if ((Get-DistroUserState) -eq "ready") { return $true }
    Start-Sleep -Seconds $every
  }
  return $false
}

function Test-Plain([string]$value, [string]$pattern) {
  # A value that goes into a command line the task keeps: it matches the pattern over its whole length and
  # holds no line break (a pattern ending in a dollar sign lets a final line break through).
  return (($value -cmatch $pattern) -and $value.IndexOf([char]10) -lt 0 -and $value.IndexOf([char]13) -lt 0)
}

function Get-ResumeArguments {
  # The switches and values of this run, as the tokens of the command line the task (or a second run) uses.
  # Empty, with $script:ResumeWhy set, when a value is not safe to hand on. The caller adds -Resume.
  $script:ResumeWhy = ""
  $t = @("-Yes")
  if ($Light) { $t += "-Light" }
  if ($Miner) { $t += "-Miner" }
  if ($Validator) { $t += "-Validator" }
  if ($NoJudge) { $t += "-NoJudge" }
  if ($NoAutoUpdate) { $t += "-NoAutoUpdate" }
  if ($PayoutLock) { $t += "-PayoutLock" }
  if ($NoKeepAlive) { $t += "-NoKeepAlive" }
  if (-not (Test-Plain $Distro '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')) { $script:ResumeWhy = "-Distro holds characters the task does not pass on"; return @() }
  $t += @("-Distro", $Distro)
  if (-not (Test-Plain $ConfigUrl '^https?://[A-Za-z0-9._~:/-]+$')) { $script:ResumeWhy = "-ConfigUrl holds characters the task does not pass on"; return @() }
  $t += @("-ConfigUrl", $ConfigUrl)
  if (-not (Test-Plain $RepoUrl '^https://[A-Za-z0-9._~:/-]+$')) { $script:ResumeWhy = "-RepoUrl holds characters the task does not pass on"; return @() }
  $t += @("-RepoUrl", $RepoUrl)
  # Checked at the top of this file, over their whole value: letters, digits, dashes and commas only.
  if ($Gpus) { $t += @("-Gpus", $Gpus) }
  if ($PayoutAddress) { $t += @("-PayoutAddress", $PayoutAddress) }
  return $t
}

function ConvertTo-TaskArguments([string[]]$tokens) {
  # Values go between double quotes, switches do not.
  $parts = @()
  foreach ($x in $tokens) { if ($x.StartsWith("-")) { $parts += $x } else { $parts += ('"' + $x + '"') } }
  return ($parts -join " ")
}

function Set-AdminOnlyRules($acl, [bool]$folder) {
  # Replaces every entry of an access list by Full control for the LocalSystem account and Administrators, and stops inheritance.
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($r in @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))) { [void]$acl.RemoveAccessRuleSpecific($r) }
  $inherit = [System.Security.AccessControl.InheritanceFlags]::None
  if ($folder) { $inherit = [System.Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit" }
  foreach ($s in $AdminSids) {
    $id = New-Object System.Security.Principal.SecurityIdentifier($s)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($id, [System.Security.AccessControl.FileSystemRights]::FullControl,
      $inherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)
    $acl.AddAccessRule($rule)
  }
}

function Test-AdminOnlyAcl($acl) {
  # Read back: protected from inheritance, owned by the LocalSystem account or Administrators, and every entry allows one of them.
  # The owner counts: an owner can always rewrite the list.
  $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier])
  if (-not $owner -or $AdminSids -notcontains $owner.Value) { return $false }
  if (-not $acl.AreAccessRulesProtected) { return $false }
  $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
  if ($rules.Count -lt 1) { return $false }
  foreach ($r in $rules) {
    if ($r.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { return $false }
    if ($AdminSids -notcontains $r.IdentityReference.Value) { return $false }
  }
  return $true
}

function Protect-AdminOnly([string]$path, [bool]$folder) {
  # The access section alone is written (Set-Acl on what Get-Acl returns writes the audit section too, which
  # needs a privilege); the list and the owner are read back and judged by Test-AdminOnlyAcl.
  $access = [System.Security.AccessControl.AccessControlSections]::Access
  $both = [System.Security.AccessControl.AccessControlSections]"Access, Owner"
  if ($folder) { $acl = [System.IO.Directory]::GetAccessControl($path, $access) } else { $acl = [System.IO.File]::GetAccessControl($path, $access) }
  Set-AdminOnlyRules $acl $folder
  if ($folder) { [System.IO.Directory]::SetAccessControl($path, $acl) } else { [System.IO.File]::SetAccessControl($path, $acl) }
  if ($folder) { $back = [System.IO.Directory]::GetAccessControl($path, $both) } else { $back = [System.IO.File]::GetAccessControl($path, $both) }
  return (Test-AdminOnlyAcl $back)
}

function Install-ResumeCopy([string]$source) {
  # "" when the copy of this file is in place and changeable by administrators alone, else why it is not.
  if (-not $source -or -not (Test-Path -LiteralPath $source -PathType Leaf)) { return "this file was not started from a file on disk, so there is nothing to copy" }
  if (-not $ResumeFile) { return "the system gives no ProgramData folder for this account" }
  $dir = Split-Path -Parent $ResumeFile
  try {
    if (Test-Path -LiteralPath $dir) {
      $item = Get-Item -LiteralPath $dir -Force -ErrorAction Stop
      if (-not $item.PSIsContainer) { return "$dir exists and is not a folder" }
      if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return "$dir is a link, not a folder" }
    } else {
      New-Item -ItemType Directory -Path $dir -ErrorAction Stop | Out-Null
    }
    if (-not (Protect-AdminOnly $dir $true)) { return "the folder $dir does not read back as changeable by administrators alone" }
    # A resumed run is the copy: it is not copied onto itself.
    if ($source -ne $ResumeFile) { Copy-Item -LiteralPath $source -Destination $ResumeFile -Force -ErrorAction Stop }
    if (-not (Protect-AdminOnly $ResumeFile $false)) { return "the copy does not read back as changeable by administrators alone" }
  } catch {
    return "the copy could not be made ($($_.Exception.Message))"
  }
  return ""
}

function Register-ResumeTask([string]$source) {
  # "" when the task is in place for the next logon, else why not. Nothing is registered unless the copy is.
  # $source is the file this run was started from.
  if ($NoResume) { return "-NoResume was given" }
  $tok = @(Get-ResumeArguments)
  if ($script:ResumeWhy) { return $script:ResumeWhy }
  $why = Install-ResumeCopy $source
  if ($why) { return $why }
  $line = '-NoProfile -ExecutionPolicy Bypass -NoExit -File "' + $ResumeFile + '" ' + (ConvertTo-TaskArguments $tok) + ' -Resume'
  $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  try {
    $existing = $null
    try { $existing = Get-ScheduledTask -TaskName $ResumeTaskName -ErrorAction Stop } catch { }
    # A resumed run disabled its own task at the start: the next restart wakes it again.
    if ($existing -and $Resume) {
      Enable-ScheduledTask -TaskName $ResumeTaskName -ErrorAction Stop | Out-Null
      return ""
    }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $line
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
    $trigger.Delay = "PT30S"
    $trigger.EndBoundary = (Get-Date).AddDays(14).ToString("s")
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 12) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -DeleteExpiredTaskAfter (New-TimeSpan -Days 1)
    Register-ScheduledTask -TaskName $ResumeTaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
  } catch {
    return "the task could not be registered ($($_.Exception.Message))"
  }
  return ""
}

function Remove-ResumeTask([bool]$onlyDisable) {
  # Quiet when there is nothing to remove. A run the task started only disables it: a task is not relied on to
  # survive its own deletion, and the 14 days of its trigger delete it.
  $found = $false
  try { if (Get-ScheduledTask -TaskName $ResumeTaskName -ErrorAction Stop) { $found = $true } } catch { }
  if (-not $found) { return }
  try {
    if ($onlyDisable) {
      Disable-ScheduledTask -TaskName $ResumeTaskName -ErrorAction Stop | Out-Null
      Say "  [i] the resume task '$ResumeTaskName' did its work: it is disabled and deletes itself in at most 14 days."
    } else {
      Unregister-ScheduledTask -TaskName $ResumeTaskName -Confirm:$false -ErrorAction Stop
      Say "  [OK] the resume task '$ResumeTaskName' is removed"
    }
  } catch {
    Warn ("the resume task '$ResumeTaskName' could not be " + $(if ($onlyDisable) { "disabled" } else { "removed" }) + ": " + $_.Exception.Message)
  }
}

function Remove-ResumeCopy([string]$running) {
  # The copy of this file kept for the task. Never the file this run is running from ($running).
  if (-not $ResumeFile -or $running -eq $ResumeFile) { return }
  if (Test-Path -LiteralPath $ResumeFile) { Remove-Item -LiteralPath $ResumeFile -Force -ErrorAction SilentlyContinue }
  $dir = Split-Path -Parent $ResumeFile
  if ((Test-Path -LiteralPath $dir) -and @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue).Count -eq 0) {
    Remove-Item -LiteralPath $dir -Force -ErrorAction SilentlyContinue
  }
}

function Restart-Self([string]$self) {
  # Runs this file ($self) again, once, in a new PowerShell with the same switches, and returns that run's exit code.
  # $null when it cannot (not started from a file, a value not safe to hand on, or this already is the second run).
  if (-not $self) { return $null }
  if ($env:DENDRA_INSTALL_REEXEC -eq "1") { return $null }
  $tok = @(Get-ResumeArguments)
  if ($script:ResumeWhy) { return $null }
  if ($Resume) { $tok += "-Resume" }
  $env:DENDRA_INSTALL_REEXEC = "1"
  & $SelfRunner -NoProfile -ExecutionPolicy Bypass -File $self @tok
  return $LASTEXITCODE
}

# One run at a time. Two runs of this file in the same distribution would both run apt, git clone, install.sh and
# join.sh on the same files, and the logon task makes two runs likely (its window opens 30 seconds after logon while
# the reader may already have started the file by hand). The lock is a Windows mutex of this logon session, taken by a
# run with -Yes before anything is read or changed and released when the run ends, on every path, also in a window
# that stays open (-NoExit): a process keeps a mutex until it releases it or ends. It is released by Exit-SingleRun,
# in the `finally` that closes the file.
$RunLockName = "DendraInstallPs1"
$script:RunLock = $null
$script:RunLockWhy = ""

function Enter-SingleRun {
  # "acquired": this run holds the lock. "busy": another run of this file holds it. "inherited": this is the second
  # run that Restart-Self starts, whose first run holds the lock until it ends. "unknown": the lock could not be asked
  # (the reason is in $script:RunLockWhy); that is not a pass.
  $script:RunLockWhy = ""
  if ($env:DENDRA_INSTALL_REEXEC -eq "1") { return "inherited" }
  $m = $null
  try { $m = New-Object System.Threading.Mutex($false, $RunLockName) } catch { $script:RunLockWhy = $_.Exception.Message; return "unknown" }
  $got = $null
  # A run that was killed leaves its mutex abandoned: the next WaitOne takes it and raises this exception to say so.
  try { $got = $m.WaitOne(0) }
  catch [System.Threading.AbandonedMutexException] { $got = $true }
  catch { $script:RunLockWhy = $_.Exception.Message; $m.Dispose(); return "unknown" }
  if ($got -eq $true) { $script:RunLock = $m; return "acquired" }
  $m.Dispose()
  if ($got -eq $false) { return "busy" }
  $script:RunLockWhy = "WaitOne gave no answer"
  return "unknown"
}

function Exit-SingleRun {
  # Releases the lock this run holds, if it holds it. Quiet: it runs at the end of every path.
  if ($null -eq $script:RunLock) { return }
  try { $script:RunLock.ReleaseMutex() } catch { }
  try { $script:RunLock.Dispose() } catch { }
  $script:RunLock = $null
}
# end resume functions

$Build = [Environment]::OSVersion.Version.Build
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$WslState = "missing"; $DistroState = "missing"; $DistroVersion = "?"; $DistroErr = ""; $SystemdState = "unknown"; $KeepAliveState = "absent"; $Driver = "missing"
$Role = "judge"; if ($Validator) { $Role = "validator" } elseif ($Miner) { $Role = "miner" }
$FreeGB = "?"; $HostRamMB = "?"; $DriveLetter = "?"; $DriveWhy = ""

function Summary([string]$action) {
  Say ("DENDRA_INSTALL_PS_SUMMARY build={0} admin={1} wsl={2} distro={3} wsl_version={4} systemd={5} driver={6} keepalive={7} disk_gb={8} host_ram_mb={9} action={10}" -f `
    $Build, $IsAdmin, $WslState, $DistroState, $DistroVersion, $SystemdState, $Driver, $KeepAliveState, $FreeGB, $HostRamMB, $action)
}

# ---------------------------------------------------------------- one run at a time
# A run with -Yes takes the lock before it reads or changes anything, so that a second run, started by hand while the
# window of the logon task is working (or the other way round), refuses here and removes neither the task nor the copy.
# A run without -Yes only prints a plan and takes none. The `finally` that releases it closes this file.
# begin single-run site
$LockState = "none"
if ($Yes) { $LockState = Enter-SingleRun }
if ($LockState -eq "busy") {
  Refuse "another run of this file is already working on this PC (a window of the task '$ResumeTaskName', or a PowerShell you started). Two runs at once would change the same Linux distribution together. Let that run finish, or close its window, then run this file again. Nothing was changed."
  Summary "refused"; exit 2
}
if ($LockState -eq "unknown") {
  Refuse "whether another run of this file is working could not be asked ($script:RunLockWhy), and not knowing is not a pass. Nothing was changed."
  Summary "not_measurable"; exit 3
}
# end single-run site
try {
# ---------------------------------------------------------------- 1. read the host
Say "== [install] reading the host =="
if ($Resume) { Say "  (resumed by the logon task after the restart)" }
$cs = Get-CimInstance Win32_ComputerSystem
$HostRamMB = [math]::Floor($cs.TotalPhysicalMemory / 1MB)
# The account. Names are not compared -- a Microsoft account reports `MicrosoftAccount\<mail>` where
# the profile is a short local name, and an RDP session reports the console user -- security ids are.
# When the console user cannot be resolved, the check says so instead of passing in silence.
$ConsoleSid = $null; $ConsoleUser = ""
if ($cs.UserName) {
  $ConsoleUser = $cs.UserName
  try { $ConsoleSid = (New-Object System.Security.Principal.NTAccount($cs.UserName)).Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $ConsoleSid = $null }
}
$MySid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$AccountCheck = "unknown (console session not resolvable)"
if ($ConsoleSid) { if ($ConsoleSid -eq $MySid) { $AccountCheck = "same account as the console session" } else { $AccountCheck = "different from the console session ($ConsoleUser)" } }

if ($Build -lt 19041) {
  Refuse "Windows build ${Build}: wsl --install needs Windows 10 version 2004 (build 19041) or Windows 11."
  Summary "refused"; exit 2
}
if (Test-Path $Wsl) {
  & $Wsl --status *> $null
  if ($LASTEXITCODE -eq 0) { $WslState = "inbox" }
  # `wsl --version` answers only on the Store build of WSL, which is the one that supports systemd.
  & $Wsl --version *> $null
  if ($LASTEXITCODE -eq 0) { $WslState = "present" }
}
if ($WslState -eq "present") {
  # `--list --verbose` carries the WSL generation of each distribution, which `--list --quiet` does
  # not: a distribution left on WSL 1 cannot run Docker, and systemd cannot be enabled in it.
  $list = WslRun @("--list", "--verbose")
  if ($script:WslRc -eq 0) {
    foreach ($line in ($list -split "`r?`n")) {
      $t = ($line.Trim() -replace '^\*\s*', '') -split '\s+'
      if ($t.Count -ge 3 -and $t[0] -eq $Distro) { $DistroState = "registered"; $DistroVersion = $t[$t.Count - 1] }
    }
  }
} elseif ($WslState -eq "inbox") {
  # The inbox build prints UTF-16 whatever WSL_UTF8 says; its answers are not read here. It is updated
  # first, and this file is run again.
  $DistroState = "unknown-until-wsl-is-updated"
}
# The drive that holds the distribution's virtual disk: registered distributions record their BasePath
# in the registry; before installation the drive is the one WSL will install to.
$DriveLetter = $env:LOCALAPPDATA.Substring(0, 1)
$DriveWhy = "where WSL installs"
if ($DistroState -eq "registered") {
  try {
    $reg = Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss" -ErrorAction Stop | ForEach-Object { Get-ItemProperty $_.PSPath } | Where-Object { $_.DistributionName -eq $Distro } | Select-Object -First 1
    if ($reg -and $reg.BasePath) {
      $bp = [string]$reg.BasePath; if ($bp.StartsWith('\\?\')) { $bp = $bp.Substring(4) }
      if ($bp -match '^([A-Za-z]):') { $DriveLetter = $Matches[1].ToUpper(); $DriveWhy = "holds $Distro's disk" }
    }
  } catch { }
}
$FreeGB = [math]::Floor((Get-PSDrive -Name $DriveLetter).Free / 1GB)

if ($DistroState -eq "registered" -and $DistroVersion -eq "2") {
  $DistroState = Get-DistroUserState
  if ($DistroState -eq "unreachable") { $DistroErr = $script:DistroProbeText }
}
if ($DistroState -eq "ready") {
  # `is-system-running` encodes the state in its exit code: `degraded` (a failed unit somewhere, common
  # under WSL) comes with a non-zero code and is a running systemd all the same. `--wait` holds until
  # startup is over, so a system still `starting` is not read as absent; `timeout` bounds that wait.
  # Read through a sentinel, anchored, so that a stray word in a wsl.exe diagnostic cannot match.
  $s = WslRun @("-d", $Distro, "--exec", "sh", "-c", 'echo sd=$(timeout 90 systemctl is-system-running --wait 2>/dev/null)')
  if ($s -match '(?m)^sd=(running|degraded)\s*$') { $SystemdState = "running" }
  elseif ($s -match '(?m)^sd=(\S+)\s*$') { $SystemdState = "off ($($Matches[1]))" }
  else { $SystemdState = "off" }
}
if (Test-Path (Join-Path $env:SystemRoot "System32\nvidia-smi.exe")) { $Driver = "present" }
try { if (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop) { $KeepAliveState = "registered" } } catch { }
$ResumeState = "absent"
try { if (Get-ScheduledTask -TaskName $ResumeTaskName -ErrorAction Stop) { $ResumeState = "registered" } } catch { }

Say "  Windows build : $Build"
Say "  administrator : $IsAdmin (account $env:USERNAME, $AccountCheck)"
Say "  memory        : $HostRamMB MB on the PC (the WSL VM gets about half unless %UserProfile%\.wslconfig sets [wsl2] memory=)"
Say "  free disk     : $FreeGB GB on ${DriveLetter}: ($DriveWhy)"
Say "  WSL           : $WslState"
Say "  distribution  : $DistroState ($Distro, WSL version $DistroVersion)"
Say "  systemd       : $SystemdState"
Say "  NVIDIA driver : $Driver (Windows side; the GPU reaches WSL through it)"
Say "  keep-alive    : $KeepAliveState"
Say "  resume task   : $ResumeState"

if (-not $IsAdmin) {
  Refuse "not running as administrator: installing WSL and registering the logon task need it. Right-click PowerShell, 'Run as administrator', then run this file again."
  Summary "refused"; exit 2
}
if ($ConsoleSid -and ($ConsoleSid -ne $MySid)) {
  Refuse "this PowerShell runs as '$env:USERNAME' while the console session belongs to '$ConsoleUser'. WSL distributions, the clone and the logon task are per account: they would land in the wrong one. Open the administrator PowerShell from the account that is logged on."
  Summary "refused"; exit 2
}

# A run with -Yes ends what an earlier run left for the restart: the task and the copy of this file. A run the task
# started disables the task instead and keeps the copy it is running from (see Remove-ResumeTask).
if ($Yes) {
  Remove-ResumeTask ([bool]$Resume)
  if (-not $Resume) { Remove-ResumeCopy $PSCommandPath }
}

# ---------------------------------------------------------------- uninstall (-Uninstall)
# It comes before the install refusals on purpose: a PC whose disk or memory no longer clears the floors
# must still be able to remove what was installed. What this file put on the Windows side is the logon
# task; what was set up inside the distribution is undone by deploy/uninstall.sh, which measures what is
# there, prints its plan, and refuses what it cannot read (a validator, keys without a verified backup).
if ($Uninstall) {
  $UnArgs = ""
  if ($Yes) { $UnArgs = " --yes" }
  if ($DeleteKeys) { $UnArgs = $UnArgs + " --delete-keys" }
  if ($KeepRegistration) { $UnArgs = $UnArgs + " --keep-registration" }
  $LinuxSide = "distribution not ready ($DistroState)"
  if ($DistroState -eq "ready") {
    # A sentinel on stdout, as for every probe of this file: the answer is a line it can match.
    $p = WslRun @("-d", $Distro, "--exec", "sh", "-c", 'if [ -r "$HOME/dendra-network/deploy/uninstall.sh" ]; then echo un=present; else echo un=absent; fi')
    if ($p -match '(?m)^un=present\s*$') { $LinuxSide = "present" }
    elseif ($p -match '(?m)^un=absent\s*$') { $LinuxSide = "no deploy/uninstall.sh in ~/dendra-network (a clone older than it, or no clone)" }
    else { $LinuxSide = "unreadable (wsl.exe answered: $p)" }
  }
  Say ""
  Say "== [uninstall] plan =="
  if ($LinuxSide -eq "present") { Say "  - inside ${Distro}: bash dendra-network/deploy/uninstall.sh$UnArgs  (its own plan is printed below)" }
  else { Say "  - inside ${Distro}: nothing run -- $LinuxSide" }
  if ($KeepAliveState -eq "registered") { Say "  - remove the logon task '$TaskName'" }
  Say "  - Not done by this file: wsl --unregister $Distro. It deletes the whole distribution, its Docker"
  Say "    volumes included -- the miner's keys among them. Run it yourself, and only once the key backup that"
  Say "    deploy/uninstall.sh writes in the distribution has been copied out (it prints its path; from Windows"
  Say "    it is under \\wsl.localhost\$Distro\)."
  # Three outcomes for the Linux side, never two. `ran`: deploy/uninstall.sh ran and its exit code is the
  # answer. `empty`: nothing of the kit can be in the distribution (none registered, or its first launch
  # never happened). `notrun`: it could not be run, and what is inside is unknown (a distribution that did
  # not answer, an inbox WSL whose answers are not read, a probe that could not be read, a clone without the
  # file). Unknown is never "clean": the logon task is what keeps a miner or a validator in there alive.
  $rc = 0; $LinuxRun = "notrun"; $NotRunRc = 3
  if ($LinuxSide -eq "present") {
    Say ""
    # The command carries no variable and no quote, like the install hand-over below.
    $ucmd = "bash dendra-network/deploy/uninstall.sh" + $UnArgs
    & $Wsl -d $Distro --cd "~" -- bash -lc $ucmd
    $rc = $LASTEXITCODE
    $LinuxRun = "ran"
  } elseif ($DistroState -eq "missing" -or $DistroState -eq "uninitialized") {
    $LinuxRun = "empty"
  } elseif ($LinuxSide -like "no deploy/uninstall.sh*") {
    # A reading: the clone is there or not, and this file has nothing to run in it. A refusal, not an unknown.
    $NotRunRc = 2
  }
  if ($LinuxRun -eq "notrun") {
    Say ""
    Say "  deploy/uninstall.sh was not run: $LinuxSide. What runs inside $Distro was not read, so the logon task"
    Say "  is kept -- it keeps alive whatever may still be running there."
    Summary ("uninstall_linux_not_run"); exit $NotRunRc
  }
  if (-not $Yes) {
    Say ""
    Say "  Nothing was done. Re-run with -Uninstall -Yes to apply the plan above."
    Summary "uninstall_planned"
    # uninstall.sh's own answer travels as it is when it is not "planned": 1 (it failed) and 3 (it could
    # not measure) are not a plan waiting for -Yes.
    if ($rc -eq 1 -or $rc -eq 3) { exit $rc }
    if ($rc -eq 0 -and $KeepAliveState -ne "registered") { exit 0 }
    exit 2
  }
  # The logon task keeps the VM -- and anything still running in it -- alive. It is removed only when
  # the Linux side came out clean: uninstall.sh ran and returned 0, or nothing of the kit can be there. A
  # refused uninstall (a validator, a backup that failed) leaves a node or a miner that still needs the VM.
  if ($rc -ne 0) {
    Say ""
    Say "  deploy/uninstall.sh exited with ${rc}: read its reason above. The logon task is kept, since what it keeps"
    Say "  alive may still be running."
    Summary ("uninstall_linux_exit_" + $rc); exit $rc
  }
  if ($KeepAliveState -eq "registered") {
    try {
      Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
      $KeepAliveState = "removed"
      Say "  [OK] logon task '$TaskName' removed"
    } catch {
      # Fail is defined with the install steps, further down: this branch says it in its own words.
      Say ("  [FAILED] removing the logon task: " + $_.Exception.Message)
      Summary "failed"; exit 1
    }
  }
  Summary "uninstalled"; exit 0
}
if ($DistroState -eq "registered" -and $DistroVersion -ne "2") {
  Refuse "$Distro runs as WSL $DistroVersion. systemd and Docker need WSL 2. Run: wsl --set-version $Distro 2 (this converts the distribution; it can take a while), then run this file again."
  Summary "refused"; exit 2
}
if ($FreeGB -lt $MinDiskGB) {
  Refuse "free disk $FreeGB GB on ${DriveLetter}: is below the miner floor of $MinDiskGB GB (DENDRA_MIN_DISK_GB). The models, the images and the first build live on this drive inside the WSL disk."
  Summary "refused"; exit 2
}
if ($Role -ne "validator") {
  Say "  [i] role: decided inside $Distro by deploy/hw_probe.sh --role, before deploy/install.sh installs anything there."
  Say "      A PC whose NVIDIA card WSL can use mines on it. Without one, the testnet runs no mining model on the CPU:"
  Say "      with enough RAM in the WSL VM (MOE_CPU_MIN_RAM_MB in deploy/hw_probe.sh) the PC joins as a judge on the"
  Say "      CPU, and -Miner is refused; with less, deploy/install.sh refuses. A judge needs the judge's disk floor"
  Say "      (DENDRA_MIN_DISK_GB_JUDGE in deploy/install.sh) on ${DriveLetter}:, a miner that cannot judge the miner floor."
}
if ([math]::Floor($HostRamMB / 2) -lt $MinRamMB) {
  Refuse "memory $HostRamMB MB: WSL gives its VM about half by default, under the $MinRamMB MB floor deploy/install.sh applies (DENDRA_MIN_RAM_MB). A larger share can be set in %UserProfile%\.wslconfig ([wsl2] memory=), but a PC this size will struggle with the first build."
  Summary "refused"; exit 2
}
if ($DistroState -eq "unreachable") {
  Refuse "wsl -d $Distro could not be asked (exit code $($script:WslRc)). What wsl.exe said:"
  Say ("         " + ($DistroErr -replace "`r?`n", "`n         "))
  Say "         Typical causes: virtualization disabled in the firmware, the Virtual Machine Platform feature off, a broken distribution. Run 'wsl -d $Distro' yourself to read the full message."
  Summary "not_measurable"; exit 3
}
# A URL travels to the distribution as an environment variable, never on a command line, so a query
# string is fine; what is refused is anything a shell could read as more than a URL.
if ($ConfigUrl -notmatch '^https?://[A-Za-z0-9._~:/%+?=&-]+$') {
  Refuse "-ConfigUrl carries characters this file will not pass on: $ConfigUrl"
  Summary "refused"; exit 2
}

# ---------------------------------------------------------------- 2. plan
$Plan = @()
if ($WslState -eq "missing")           { $Plan += "install WSL and $Distro : wsl --install -d $Distro --no-launch  (then restart Windows and run this file again)" }
elseif ($WslState -eq "inbox")         { $Plan += "update WSL to the Store build (systemd needs it) : wsl --update  (then run this file again)" }
elseif ($DistroState -eq "missing")    { $Plan += "install $Distro : wsl --install -d $Distro --no-launch" }
if ($WslState -eq "missing" -and -not $NoResume) { $Plan += "before that restart: copy this file where only administrators can change it and register the one-time logon task '$ResumeTaskName', which runs it again with these options at your next logon (-NoResume skips it)" }
if ($WslState -eq "inbox") { $Plan += "after the update, this file starts itself again once in the same window" }
if ($WslState -eq "present" -and $DistroState -ne "ready") { $Plan += "open $Distro once from the Start menu to create its Linux user, then run this file again" }
if ($DistroState -eq "ready" -and $SystemdState -ne "running") { $Plan += "enable systemd in $Distro (/etc/wsl.conf [boot] systemd=true), then wsl --shutdown" }
if ($DistroState -eq "ready")          { $Plan += "install git and ssh-keygen in $Distro if missing, clone $RepoUrl into ~/dendra-network there, with a marker that tells install.sh the clone was made for this run (a clone already there is fetched), and copy deploy/install.sh, with its probe and pins, into the clone's .git (dendra-installer): of main for a first installation, of the newest release tag its recorded signers verify for a kit that recorded them" }
if (-not $NoKeepAlive -and $KeepAliveState -ne "registered") { $Plan += "after install.sh, unless it refuses this PC: register the logon task '$TaskName' (keeps the WSL VM alive while you are logged on)" }
$flags = "--yes"
if ($Light) { $flags += " --light" }
if ($Validator) { $flags += " --validator" } elseif ($Miner) { $flags += " --miner" }
# The three kit switches (--no-judge, --no-auto-update, --payout-lock), relayed as they are to install.sh, which relays them to join.sh.
if ($NoJudge) { $flags += " --no-judge" }
if ($NoAutoUpdate) { $flags += " --no-auto-update" }
if ($PayoutLock) { $flags += " --payout-lock" }
# Checked above: letters and digits only, so it can sit in the command string.
if ($PayoutAddress) { $flags += " --payout-address " + $PayoutAddress }
if ($Gpus) {
  $flags += " --gpus " + $Gpus
  Warn "-Gpus: one miner identity per card, each with its own stake, faucet drip and 24-word phrase. Under WSL 2 this is not measured on real multi-card hardware: the hourly pinning check (H6 of miner_health.sh) says whether each card held."
}
if ($DistroState -eq "ready") { $Plan += ("run inside {0}: CONFIG_URL={1} bash dendra-network/.git/dendra-installer/install.sh {2}  (disk read on {3}:)" -f $Distro, $ConfigUrl, $flags, $DriveLetter) }
if ($Driver -eq "missing") { Warn "no nvidia-smi.exe on Windows: without the NVIDIA Windows driver the GPU is invisible from WSL, and no mining model runs on the CPU: this PC then joins as a judge on the CPU if the WSL VM has the RAM deploy/hw_probe.sh asks for (MOE_CPU_MIN_RAM_MB), and is refused otherwise. Install the driver from nvidia.com first if this PC has an NVIDIA card." }
Say ""
Say "== [install] plan =="
foreach ($p in $Plan) { Say "  - $p" }
if (-not $Yes) {
  Say ""
  Say "  Nothing was done. Re-run with -Yes to apply the plan above."
  Summary "planned"; exit 2
}

# ---------------------------------------------------------------- 3. apply, stopping where a human is needed
function Step([string]$m) { Say ""; Say "== [install] $m ==" }
function Fail([string]$m) { Say "  [FAILED] $m"; Summary "failed"; exit 1 }

if ($WslState -eq "missing") {
  Step "installing WSL and $Distro"
  & $Wsl --install -d $Distro --no-launch
  if ($LASTEXITCODE -ne 0) { Fail "wsl --install returned $LASTEXITCODE. If it hangs at 0%, try: wsl --install --web-download -d $Distro" }
  Say ""
  $why = Register-ResumeTask $PSCommandPath
  if ($why) {
    Say "  WSL is installed. Restart Windows now, then run this file again: it continues from here."
    Say "  It will not resume by itself: $why."
  } else {
    Say "  WSL is installed. Restart Windows now. At your next logon a window opens by itself and this installation continues."
    Say "  It is the one-time task '$ResumeTaskName', run as you with administrator rights from a copy of this file that only administrators can change."
    Say ('  To cancel it, in an administrator PowerShell: Unregister-ScheduledTask -TaskName "{0}" -Confirm:$false' -f $ResumeTaskName)
  }
  Summary "restart_required"; exit 0
}
if ($WslState -eq "inbox") {
  Step "updating WSL to the Store build"
  & $Wsl --update
  if ($LASTEXITCODE -ne 0) { Fail "wsl --update returned $LASTEXITCODE. Install 'Windows Subsystem for Linux' from the Microsoft Store, then run this file again." }
  & $Wsl --shutdown
  & $Wsl --version *> $null
  if ($LASTEXITCODE -ne 0) { Fail "WSL still answers as the inbox build after the update. Install it from the Microsoft Store, then run this file again." }
  Say ""
  $WslState = "present"
  $again = Restart-Self $PSCommandPath
  if ($null -ne $again) { exit $again }
  Say "  WSL is now the Store build. Run this file again: it re-reads the distribution with it."
  Summary "rerun_required"; exit 0
}
if ($DistroState -eq "missing") {
  Step "installing $Distro"
  & $Wsl --install -d $Distro --no-launch
  if ($LASTEXITCODE -ne 0) { Fail "wsl --install -d $Distro returned $LASTEXITCODE (try --web-download)" }
  $DistroState = "uninitialized"
}
if ($DistroState -eq "uninitialized") {
  Say ""
  Say "  Open '$Distro' from the Start menu once. It asks for a Linux user name and password - that is the"
  if ($Resume) {
    # A window the task opened has nobody to run it again: it waits for the account, then goes on.
    Say "  account the miner runs under. This window waits for it, up to 30 minutes, and then goes on by itself."
    if (Wait-DistroAccount 1800 15) { $DistroState = "ready" }
  } else {
    Say "  account the miner runs under. Close it when the prompt appears, then run this file again."
  }
  if ($DistroState -ne "ready") {
    if ($Resume) { Say "  No account appeared in 30 minutes. Run this file again once you have opened $Distro." }
    Summary "first_launch_required"; exit 0
  }
}

if ($SystemdState -ne "running") {
  Step "enabling systemd in $Distro"
  $rc = WslSh "root" @'
set -e
if grep -qs '^systemd=true' /etc/wsl.conf; then
  echo unchanged
else
  if grep -qs '^\[boot\]' /etc/wsl.conf; then
    sed -i 's/^\[boot\]/[boot]\nsystemd=true/' /etc/wsl.conf
  else
    printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf
  fi
  echo changed
fi
'@
  if ($rc -ne 0) { Fail "could not write /etc/wsl.conf in $Distro" }
  # Whether or not the file changed, systemd was not seen running: the VM is restarted so the setting
  # is read, then the state is asked again through the same sentinel.
  & $Wsl --shutdown
  $s = WslRun @("-d", $Distro, "--exec", "sh", "-c", 'echo sd=$(timeout 120 systemctl is-system-running --wait 2>/dev/null)')
  if (-not ($s -match '(?m)^sd=(running|degraded)\s*$')) { Fail "systemd is still not running in $Distro after the restart (probe answered: '$s')" }
  $SystemdState = "running"
}

Step "git and the repository inside $Distro"
$rc = WslSh "root" @'
set -e
if ! command -v git >/dev/null 2>&1 || ! command -v ssh-keygen >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq git ca-certificates curl openssh-client
fi
'@
if ($rc -ne 0) { Fail "installing git in $Distro" }
# The marker of the clone this file makes (deploy/install.sh, "A clone made by install.ps1"): this run's token and
# the commit cloned, in the clone's .git, where git never looks. install.sh counts the clone as made for its run only
# while the clone is still as git clone left it, and removes the marker once the clone sits on its release tag. A
# clone that still carries the marker (an earlier run stopped before that) gets the token of this run.
# The installer this file runs is copied with the probe and the pins next to it into the clone's .git
# (dendra-installer), where git never looks: the commit just cloned (main), or, for a clone already there, main
# fetched from $RepoUrl, unless that kit recorded its docker/RELEASE_SIGNERS (DENDRA_RELEASE_SIGNERS_SHA256 in the
# .env of its miner kit or node kit) and the clone still holds that very file: its installer is then taken from the
# newest release tag of $RepoUrl that this file verifies, with the same header check as deploy/install.sh::tag_adopt,
# because main carries no signature and this code runs with sudo. A tag that does not verify stops the run: no
# older tag is taken in its place. A clone without a record has no trust root yet and takes main, as a first
# installation does. Never the clone's own deploy/install.sh: once install.sh has placed the clone on an older
# release (during a governed upgrade, main is ahead of the network), that file is the older release's installer,
# and the installer of v0.2.2 moves a clone already present to main.
$FreshToken = [guid]::NewGuid().ToString("N")
$clone = @'
set -e
d="$HOME/dendra-network"; m="$d/.git/dendra-fresh-clone"; x="$d/.git/dendra-installer"
if [ -d "$d/.git" ]; then
  echo "clone already present: $d"
  if [ -f "$m" ]; then
    sed -i 's/^token=.*/token=FRESH_TOKEN/' "$m"
    echo "it carries the marker of a clone this file made: install.sh checks that it is still as git clone left it"
  fi
  s="$d/docker/RELEASE_SIGNERS"; rec=""; have=""
  for e in "$d/deploy/testnet-miner/.env" "$d/deploy/testnet-node/.env"; do
    [ -r "$e" ] || continue
    rec="$rec $(sed -n 's/^DENDRA_RELEASE_SIGNERS_SHA256=//p' "$e" | head -1 | tr -cd '0-9a-f')"
  done
  [ -r "$s" ] && have="$(sha256sum "$s" | cut -d' ' -f1)"
  if [ -n "$have" ] && [ -n "$(echo $rec)" ]; then
    for v in $rec; do
      if [ "$v" != "$have" ]; then
        echo "refused: docker/RELEASE_SIGNERS of this clone (sha256 $have) is not the file this kit recorded ($v): the trust root changed under the kit, and the installer is not taken from a tag verified with another file. deploy/join.sh --accept-new-signers records a new one on purpose. Nothing was changed."
        exit 1
      fi
    done
    if ! command -v ssh-keygen >/dev/null 2>&1; then
      echo "ssh-keygen is not installed, so the signature of the release tag the installer comes from cannot be verified: apt-get install openssh-client, then run this file again. Nothing was changed."
      exit 1
    fi
    ns=refs/dendra-ps1-tags
    t="$(git -C "$d" ls-remote --tags REPO_URL | awk '$2 != "" { print $2 }' | sed -n 's#^refs/tags/##p' | grep -E '^v[0-9]+[.][0-9]+[.][0-9]+$' | sort -rV | head -1)"
    if [ -z "$t" ]; then
      echo "no release tag of REPO_URL could be listed: this kit recorded its signers, so the installer is not taken from main. Nothing was changed."
      exit 1
    fi
    git -C "$d" fetch -q --no-tags REPO_URL "+refs/tags/${t}:${ns}/${t}"
    th="$(git -C "$d" cat-file tag "$ns/$t" 2>/dev/null | awk '$1 == "" { exit } $1 == "tag" { print $2; exit }')"
    ty="$(git -C "$d" cat-file tag "$ns/$t" 2>/dev/null | awk '$1 == "" { exit } $1 == "type" { print $2; exit }')"
    if [ "$th" != "$t" ] || [ "$ty" != commit ]; then
      git -C "$d" update-ref -d "$ns/$t"
      echo "refused: the tag $t that REPO_URL publishes is not a release tag object of that name (its header names ${th:-no tag}, of type ${ty:-unread}): the signed object of another release published under a newer name is not followed. Nothing was changed."
      exit 1
    fi
    if ! git -C "$d" -c gpg.program=/bin/false -c gpg.ssh.allowedSignersFile="$s" verify-tag "$ns/$t" >/dev/null 2>&1; then
      git -C "$d" update-ref -d "$ns/$t"
      echo "refused: the tag $t does not verify with the docker/RELEASE_SIGNERS this kit recorded, and the installer is not taken from main or from an older tag in its place. Nothing was changed."
      exit 1
    fi
    r="$ns/$t"; src="the release tag $t, verified with the signers this kit recorded"
  else
    git -C "$d" fetch -q REPO_URL main
    r=FETCH_HEAD; src="main (this clone has no recorded signers: there is no trust root to verify a tag with)"
  fi
else
  git clone REPO_URL "$d"
  { echo "token=FRESH_TOKEN"; echo "head=$(git -C "$d" rev-parse HEAD)"; } > "$m"
  r=HEAD; src="main (a first installation)"
fi
rm -rf "$x"; mkdir "$x"
for f in install.sh hw_probe.sh kit_pins.txt; do git -C "$d" show "${r}:deploy/$f" > "$x/$f"; done
echo "installer: deploy/install.sh of $src at $(git -C "$d" rev-parse "$r"), copied to $x"
case "$r" in refs/*) git -C "$d" update-ref -d "$r" ;; esac
'@
$clone = $clone.Replace("REPO_URL", $RepoUrl).Replace("FRESH_TOKEN", $FreshToken)
$rc = WslSh $null $clone
if ($rc -ne 0) { Fail "git clone or git fetch of $RepoUrl in $Distro, or the copy of the installer into the clone's .git (main for a first installation; the newest verified release tag for a kit that recorded its signers)" }

Step "handing over to deploy/install.sh inside $Distro"
Say "  from here on the output is install.sh's, then join.sh's. sudo inside $Distro may ask for your Linux password."
Say ""
# CONFIG_URL and the drive to measure cross into the distribution as environment variables (WSLENV),
# never on the command line: an argument under `--` is read once more by the login shell before
# bash -lc sees it, and a URL with a query string would be split there. The command below carries no
# variable and no quote. The drive is the one read above, so both files judge the same disk.
$env:CONFIG_URL = $ConfigUrl
$env:DENDRA_WSL_HOST_MOUNT = "/mnt/" + $DriveLetter.ToLower()
# The token of the clone's marker crosses the same way; install.sh reads it only against a marker that carries it.
$env:DENDRA_FRESH_CLONE = $FreshToken
$share = "CONFIG_URL/u:DENDRA_WSL_HOST_MOUNT/u:DENDRA_FRESH_CLONE/u"
if ($env:WSLENV) { $env:WSLENV = $env:WSLENV + ":" + $share } else { $env:WSLENV = $share }
$cmd = "bash dendra-network/.git/dendra-installer/install.sh " + $flags
& $Wsl -d $Distro --cd "~" -- bash -lc $cmd
$rc = $LASTEXITCODE

# The logon task comes after the role, which install.sh decides (deploy/hw_probe.sh --role): registered earlier,
# it would leave a PC refused there (no usable NVIDIA card and a WSL VM below MOE_CPU_MIN_RAM_MB) with a
# hidden task holding its VM up at every logon for nothing. A refusal (exit 2)
# registers nothing; any other outcome may have left something running in the VM, and the task keeps it alive.
$TaskFailed = ""
if ($rc -ne 2 -and -not $NoKeepAlive -and $KeepAliveState -ne "registered") {
  Step "registering the logon task '$TaskName'"
  # A process that never exits keeps the WSL VM from being shut down as idle. The task starts it at
  # logon, hidden, as the current user, with no time limit, and starts it again whenever it ends (a
  # `wsl --shutdown` from elsewhere ends it). Remove it with:
  #   Unregister-ScheduledTask -TaskName "Dendra WSL keep-alive" -Confirm:$false
  $inner = "while(1){{& '{0}' -d {1} --exec sleep infinity; Start-Sleep 30}}" -f $Wsl, $Distro
  $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoProfile -WindowStyle Hidden -Command "' + $inner + '"')
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $KeepAliveState = "registered"
  } catch { $TaskFailed = $_.Exception.Message; Say ("  [FAILED] registering the logon task: " + $TaskFailed) }
}
# A task that could not be registered after a successful install is a failed step (exit 1); after another outcome,
# that outcome's own code is kept, and the line above says the task is missing too.
if ($rc -eq 0 -and $TaskFailed) { Summary "failed"; exit 1 }

# Health, from Windows. The hourly check runs inside the distribution and writes its alert file there: nothing on
# the Windows desktop shows it. So the command that reads it, and where the alert lives, are printed here, for
# every outcome that may have left something running (not on a refusal, which started nothing).
$HealthScript = "dendra-network/deploy/testnet-miner/miner_health.sh"
$HealthAlert = "dendra-network/deploy/testnet-miner/miner-health.ALERT"
if ($Validator) {
  $HealthScript = "dendra-network/deploy/validator_health.sh"
  $HealthAlert = "dendra-network/deploy/testnet-node/validator-health.ALERT"
}
$HealthLines = @(
  "",
  "  Health, from Windows (the hourly check runs inside $Distro, and nothing on the Windows desktop shows it):",
  ("    wsl -d {0} --cd ~ -- bash -lc 'bash {1}'" -f $Distro, $HealthScript),
  ("    its alert file exists only while something is wrong: ~/{0} inside {1}" -f $HealthAlert, $Distro),
  ("    (in File Explorer: \\wsl.localhost\{0}, then your Linux home folder)" -f $Distro)
)

if ($rc -ne 0) {
  Say ""
  # install.sh answers with the same three codes as this file; they are passed on, not folded into 1.
  switch ($rc) {
    2 { Say "  install.sh refused (exit 2): read its reason above. The refused step changed nothing, and this run registered no logon task." }
    3 { Say "  install.sh could not measure (exit 3): the host, or the miner's health once join.sh started it (it keeps running and registering). Read the reason above." }
    default { Say "  install.sh exited with ${rc}: a step failed. Windows is prepared; read the output above, then re-run just it:"; Say ("    wsl -d {0} --cd ~ -- bash -lc 'CONFIG_URL={1} DENDRA_WSL_HOST_MOUNT={2} DENDRA_FRESH_CLONE={4} {3}'" -f $Distro, $ConfigUrl, $env:DENDRA_WSL_HOST_MOUNT, $cmd, $FreshToken) }
  }
  if ($rc -ne 2) { $HealthLines | ForEach-Object { Say $_ } }
  Summary ("linux_side_exit_" + $rc); exit $rc
}
$HealthLines | ForEach-Object { Say $_ }
Summary "joined"
exit 0
} finally { Exit-SingleRun }
