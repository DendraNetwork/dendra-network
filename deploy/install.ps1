<#
install.ps1 - prepare a Windows PC for a Dendra miner: WSL 2 + Ubuntu, then deploy/install.sh inside it.

WHAT THIS FILE IS FOR. On Windows the miner runs inside WSL 2, and getting there by hand means: install
WSL, install a distribution, create its user, enable systemd, restart WSL, install Docker and the NVIDIA
toolkit inside, clone the repository, and only then run join.sh. This file walks that path and stops at
the first step that needs you (a restart, the first launch of the distribution). It is not clever: it
runs the documented Microsoft commands and then hands over to deploy/install.sh, which prepares the
Linux side and hands over to join.sh in turn.

WHAT IT DOES, IN THIS ORDER, AND NOTHING ELSE:
  1. reads the host: Windows build, administrator rights, the account, memory, free disk on the drive
     that holds the distribution, WSL itself, the distribution and its WSL version, systemd, the NVIDIA
     driver;
  2. installs WSL and the distribution when missing (wsl --install -d <Distro> --no-launch); a RESTART
     is then required, and this file says so and stops -- run it again afterwards;
  3. asks you to open the distribution once from the Start menu when its Linux user does not exist yet
     (that first launch is where the user and password are created), and stops;
  4. enables systemd in the distribution (/etc/wsl.conf, [boot] systemd=true) and restarts WSL, because
     Docker inside WSL is a systemd service;
  5. installs git inside the distribution and clones the repository into ~/dendra-network there;
  6. runs bash deploy/install.sh --yes inside the distribution (see that file for what it does: it decides the
     ROLE of this PC with deploy/hw_probe.sh --role before it changes anything);
  7. registers a logon task that keeps the WSL virtual machine alive, UNLESS install.sh refused this PC (exit 2):
     a PC with no role gets no hidden task keeping a VM up for nothing. WSL stops its VM when the last session
     closes; a miner inside it stops with it, and nothing on screen says so. -NoKeepAlive skips it;
  8. prints the command that reads this PC's health from Windows, and where its alert file lives: the hourly
     check runs inside the distribution, where nothing on the Windows desktop shows it.

IT CHANGES NOTHING WITHOUT -Yes: run it once without the switch and it prints the plan.
IT IS NOT MEANT TO BE RUN FROM A URL. Download it, read it, then run it from an ADMINISTRATOR PowerShell
opened from YOUR OWN account (WSL, the clone and the logon task belong to the account that runs this):

  powershell -ExecutionPolicy Bypass -File .\install.ps1            # plan only
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Yes       # do it
  ... -Yes -Light        read the chain from the public RPC instead of running a node (join.sh --remote-rpc)
  ... -Yes -Miner        mine without asking for the judge role (needs an NVIDIA card WSL can use: no mining
                         model runs on the CPU; deploy/hw_probe.sh --role decides)      ... -Yes -Validator
  ... -NoKeepAlive       do not register the logon task              ... -Distro <name>   (default Ubuntu)
  ... -Yes -PayoutAddress dendra1...   where the Final Testnet Season pays this miner (install.sh --payout-address,
                         then join.sh, which verifies its checksum before anything changes; a key NOT on this PC)
  ... -Yes -Gpus all     one miner identity PER NVIDIA CARD (install.sh --gpus, then join.sh --gpus; also a
                         list of card indices, 0,2, or of card UUIDs). NOT MEASURED on real multi-card
                         hardware under WSL 2: there each card is held to its identity by CUDA_VISIBLE_DEVICES,
                         and the hourly self-test's pinning check (H6) says whether it held.

THE DISK FLOOR CHECKED HERE IS THE MINER'S. Whether this PC can judge is decided by the RAM of the WSL
VM, which cannot be read before WSL exists; deploy/install.sh asks deploy/hw_probe.sh inside the
distribution, before installing anything, and applies the judge's higher floor only to a host that can
judge.

UNINSTALL, THE SAME WAY: plan first, nothing without -Yes.
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall              # plan only
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall -Yes         # do it
  ... -Uninstall -Yes -DeleteKeys   also delete the miner's keys, AFTER a verified backup in the same run
  ... -Uninstall -Yes -KeepRegistration   a miner still registered (or whose registration cannot be read):
                                          remove the kit anyway and keep its keys (uninstall.sh --keep-registration)
It runs deploy/uninstall.sh inside the distribution (see that file for what it removes and what it
keeps: the miner's keys always, unless -DeleteKeys), then removes the logon task. It never unregisters
the distribution: `wsl --unregister` deletes everything in it, Docker volumes and keys included, and is
printed for you to run yourself once the backup is copied out.

NO INBOUND PORT IS OPENED, AND NONE IS NEEDED FOR A MINER: it dials out to the relay, the RPC and the
faucet. A home connection behind carrier-grade NAT qualifies; no router page is involved.

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
  [switch]$Validator,
  [switch]$NoKeepAlive,
  [switch]$Uninstall,
  [switch]$DeleteKeys,
  [switch]$KeepRegistration,
  [string]$Distro = "Ubuntu",
  [string]$ConfigUrl = "https://testnet-api.dendranetwork.com/network-info.txt",
  [string]$RepoUrl = "https://github.com/DendraNetwork/dendra-network.git",
  [string]$Gpus = "",
  [string]$PayoutAddress = ""
)

# THE PAYOUT ADDRESS: -PayoutAddress is relayed to install.sh --payout-address, which relays it to join.sh. It
# travels inside a command line built as a string, so it is checked HERE first, as install.sh checks it: letters
# and digits only, the WHOLE value (\z: a final line break does not end it, as $ would let it), never printed when
# refused; a validator is paid nothing by the season. Its checksum is join.sh's to verify, before anything changes.
if ($PayoutAddress -and ($PayoutAddress -cnotmatch '^[A-Za-z0-9]{1,128}\z')) {
  Write-Host "  [REFUSED] -PayoutAddress holds characters no address has: it is not passed on (the value is not printed). Nothing was changed."
  exit 2
}
if ($PayoutAddress -and $Validator) {
  Write-Host "  [REFUSED] -PayoutAddress applies to a miner, not to -Validator. Nothing was changed."
  exit 2
}

# ONE IDENTITY PER CARD: -Gpus is relayed to install.sh --gpus, which relays it to join.sh. It travels inside a
# command line built as a string, so it is checked HERE first, case-sensitively, by the expression both of
# those files apply (GPUS_SPEC_RE); a validator is one node, never one per card.
if ($Gpus -and ($Gpus -cnotmatch '^(all|[0-9]+(,[0-9]+)*|GPU-[0-9a-f-]+(,GPU-[0-9a-f-]+)*)$')) {
  Write-Host "  [REFUSED] -Gpus takes all, a list of card indices (0,2) or a list of card UUIDs (GPU-...). Nothing was changed."
  exit 2
}
if ($Gpus -and $Validator) {
  Write-Host "  [REFUSED] -Gpus runs one MINER identity per card; a validator is one node. Nothing was changed."
  exit 2
}

$ErrorActionPreference = "Continue"
# wsl.exe prints UTF-16 by default; with this variable the Store build prints UTF-8, which PowerShell
# 5.1 reads as text. The inbox build ignores it, which is one reason the inbox build is updated first.
$env:WSL_UTF8 = "1"
$Wsl = Join-Path $env:SystemRoot "System32\wsl.exe"
$TaskName = "Dendra WSL keep-alive"
# The MINER floors of deploy/install.sh, read here on the Windows drive that holds the distribution's
# disk: inside the VM `df` sees a sparse virtual disk, not the space left on that drive. The judge's
# higher disk floor is NOT applied here: it only concerns a host that can judge, which is decided by the
# RAM of the WSL VM -- unreadable before WSL exists. deploy/install.sh decides it inside the
# distribution with deploy/hw_probe.sh, before it installs anything, on this same drive (the drive is
# passed to it below).
$MinDiskGB = 12; if ($env:DENDRA_MIN_DISK_GB) { $MinDiskGB = [int]$env:DENDRA_MIN_DISK_GB }
$MinRamMB = 4000; if ($env:DENDRA_MIN_RAM_MB) { $MinRamMB = [int]$env:DENDRA_MIN_RAM_MB }

function Say([string]$m) { Write-Host $m }
function Warn([string]$m) { Write-Host "  [!] $m" }
function Refuse([string]$m) { Write-Host "  [REFUSED] $m" }
$script:WslRc = 0
function WslRun([string[]]$a) {
  # Runs wsl.exe and returns what it printed (stdout and stderr), whatever the exit code; the code is
  # left in $script:WslRc. The text is for humans and for SENTINELS: decisions below never rest on
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
  # Runs a shell script INSIDE the distribution and returns its exit code. The script travels as ONE
  # base64 argument: nothing is piped, so the CR LF and the byte-order mark that PowerShell 5.1 appends
  # to a piped string never reach sh, and `--exec` keeps the login shell from re-reading the text. The
  # `tr -d` also strips a CR that a Windows checkout of THIS file could have put into the here-strings.
  $b = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($script))
  $cmd = "echo $b | base64 -d | tr -d '\r' | sh -s"
  if ($user) { & $Wsl -d $Distro -u $user --exec sh -c $cmd | Out-Host } else { & $Wsl -d $Distro --exec sh -c $cmd | Out-Host }
  return $LASTEXITCODE
}

$Build = [Environment]::OSVersion.Version.Build
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$WslState = "missing"; $DistroState = "missing"; $DistroVersion = "?"; $DistroErr = ""; $SystemdState = "unknown"; $KeepAliveState = "absent"; $Driver = "missing"
$Role = "judge"; if ($Validator) { $Role = "validator" } elseif ($Miner) { $Role = "miner" }
$FreeGB = "?"; $HostRamMB = "?"; $DriveLetter = "?"; $DriveWhy = ""

function Resume([string]$action) {
  Say ("DENDRA_INSTALL_PS_RESUME build={0} admin={1} wsl={2} distro={3} wsl_version={4} systemd={5} driver={6} keepalive={7} disk_gb={8} host_ram_mb={9} action={10}" -f `
    $Build, $IsAdmin, $WslState, $DistroState, $DistroVersion, $SystemdState, $Driver, $KeepAliveState, $FreeGB, $HostRamMB, $action)
}

# ---------------------------------------------------------------- 1. read the host
Say "== [install] reading the host =="
$cs = Get-CimInstance Win32_ComputerSystem
$HostRamMB = [math]::Floor($cs.TotalPhysicalMemory / 1MB)
# The account. Names are not compared -- a Microsoft account reports `MicrosoftAccount\<mail>` where
# the profile is a short local name, and an RDP session reports the CONSOLE user -- security ids are.
# When the console user cannot be resolved, the check says so instead of passing in silence.
$ConsoleSid = $null; $ConsoleUser = ""
if ($cs.UserName) {
  $ConsoleUser = $cs.UserName
  try { $ConsoleSid = (New-Object System.Security.Principal.NTAccount($cs.UserName)).Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $ConsoleSid = $null }
}
$MySid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$AccountCheck = "unknown (console session not resolvable)"
if ($ConsoleSid) { if ($ConsoleSid -eq $MySid) { $AccountCheck = "same account as the console session" } else { $AccountCheck = "DIFFERENT from the console session ($ConsoleUser)" } }

if ($Build -lt 19041) {
  Refuse "Windows build ${Build}: wsl --install needs Windows 10 version 2004 (build 19041) or Windows 11."
  Resume "refused"; exit 2
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
  # A distribution is usable once its first launch created a regular user (uid 1000). Before that,
  # `wsl -d <Distro>` starts the setup dialog, which this file must not drive blindly. The answer is
  # a SENTINEL on stdout -- `uid1000=<name>` or `uid1000=` -- never the mere presence of output.
  $u = WslRun @("-d", $Distro, "-u", "root", "--exec", "sh", "-c", 'echo uid1000=$(getent passwd 1000 | cut -d: -f1)')
  if ($u -match '(?m)^uid1000=(\S+)\s*$') { $DistroState = "ready" }
  elseif ($u -match '(?m)^uid1000=\s*$') { $DistroState = "uninitialized" }
  else { $DistroState = "unreachable"; $DistroErr = $u }
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

Say "  Windows build : $Build"
Say "  administrator : $IsAdmin (account $env:USERNAME, $AccountCheck)"
Say "  memory        : $HostRamMB MB on the PC (the WSL VM gets about half unless %UserProfile%\.wslconfig sets [wsl2] memory=)"
Say "  free disk     : $FreeGB GB on ${DriveLetter}: ($DriveWhy)"
Say "  WSL           : $WslState"
Say "  distribution  : $DistroState ($Distro, WSL version $DistroVersion)"
Say "  systemd       : $SystemdState"
Say "  NVIDIA driver : $Driver (Windows side; the GPU reaches WSL through it)"
Say "  keep-alive    : $KeepAliveState"

if (-not $IsAdmin) {
  Refuse "not running as administrator: installing WSL and registering the logon task need it. Right-click PowerShell, 'Run as administrator', then run this file again."
  Resume "refused"; exit 2
}
if ($ConsoleSid -and ($ConsoleSid -ne $MySid)) {
  Refuse "this PowerShell runs as '$env:USERNAME' while the console session belongs to '$ConsoleUser'. WSL distributions, the clone and the logon task are per account: they would land in the wrong one. Open the administrator PowerShell from the account that is logged on."
  Resume "refused"; exit 2
}

# ---------------------------------------------------------------- uninstall (-Uninstall)
# It comes BEFORE the install refusals on purpose: a PC whose disk or memory no longer clears the floors
# must still be able to remove what was installed. What this file put on the Windows side is the logon
# task; what was set up inside the distribution is undone by deploy/uninstall.sh, which MEASURES what is
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
  Say "  - NOT done by this file: wsl --unregister $Distro. It deletes the whole distribution, its Docker"
  Say "    volumes included -- the miner's keys among them. Run it yourself, and only once the key backup that"
  Say "    deploy/uninstall.sh writes in the distribution has been copied out (it prints its path; from Windows"
  Say "    it is under \\wsl.localhost\$Distro\)."
  # THREE OUTCOMES FOR THE LINUX SIDE, NEVER TWO. `ran`: deploy/uninstall.sh ran and its exit code is the
  # answer. `empty`: nothing of the kit can be in the distribution (none registered, or its first launch
  # never happened). `notrun`: it could not be run, and what is inside is UNKNOWN -- a distribution that did
  # not answer, an inbox WSL whose answers are not read, a probe that could not be read, a clone without the
  # file. An unknown is never "clean": the logon task is what keeps a miner or a validator in there alive.
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
    Say "  deploy/uninstall.sh was NOT run: $LinuxSide. What runs inside $Distro was not read, so the logon task"
    Say "  is KEPT -- it keeps alive whatever may still be running there."
    Resume ("uninstall_linux_not_run"); exit $NotRunRc
  }
  if (-not $Yes) {
    Say ""
    Say "  Nothing was done. Re-run with -Uninstall -Yes to apply the plan above."
    Resume "uninstall_planned"
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
    Say "  deploy/uninstall.sh exited with ${rc}: read its reason above. The logon task is KEPT, since what it keeps"
    Say "  alive may still be running."
    Resume ("uninstall_linux_exit_" + $rc); exit $rc
  }
  if ($KeepAliveState -eq "registered") {
    try {
      Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
      $KeepAliveState = "removed"
      Say "  [OK] logon task '$TaskName' removed"
    } catch {
      # Fail is defined with the install steps, further down: this branch says it in its own words.
      Say ("  [FAILED] removing the logon task: " + $_.Exception.Message)
      Resume "failed"; exit 1
    }
  }
  Resume "uninstalled"; exit 0
}
if ($DistroState -eq "registered" -and $DistroVersion -ne "2") {
  Refuse "$Distro runs as WSL $DistroVersion. systemd and Docker need WSL 2. Run: wsl --set-version $Distro 2 (this converts the distribution; it can take a while), then run this file again."
  Resume "refused"; exit 2
}
if ($FreeGB -lt $MinDiskGB) {
  Refuse "free disk $FreeGB GB on ${DriveLetter}: is below the miner floor of $MinDiskGB GB (DENDRA_MIN_DISK_GB). The models, the images and the first build live on this drive inside the WSL disk."
  Resume "refused"; exit 2
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
  Resume "refused"; exit 2
}
if ($DistroState -eq "unreachable") {
  Refuse "wsl -d $Distro could not be asked (exit code $($script:WslRc)). What wsl.exe said:"
  Say ("         " + ($DistroErr -replace "`r?`n", "`n         "))
  Say "         Typical causes: virtualization disabled in the firmware, the Virtual Machine Platform feature off, a broken distribution. Run 'wsl -d $Distro' yourself to read the full message."
  Resume "not_measurable"; exit 3
}
# A URL travels to the distribution as an environment variable, never on a command line, so a query
# string is fine; what is refused is anything a shell could read as more than a URL.
if ($ConfigUrl -notmatch '^https?://[A-Za-z0-9._~:/%+?=&-]+$') {
  Refuse "-ConfigUrl carries characters this file will not pass on: $ConfigUrl"
  Resume "refused"; exit 2
}

# ---------------------------------------------------------------- 2. plan
$Plan = @()
if ($WslState -eq "missing")           { $Plan += "install WSL and $Distro : wsl --install -d $Distro --no-launch  (then RESTART Windows and run this file again)" }
elseif ($WslState -eq "inbox")         { $Plan += "update WSL to the Store build (systemd needs it) : wsl --update  (then run this file again)" }
elseif ($DistroState -eq "missing")    { $Plan += "install $Distro : wsl --install -d $Distro --no-launch" }
if ($WslState -eq "present" -and $DistroState -ne "ready") { $Plan += "open $Distro once from the Start menu to create its Linux user, then run this file again" }
if ($DistroState -eq "ready" -and $SystemdState -ne "running") { $Plan += "enable systemd in $Distro (/etc/wsl.conf [boot] systemd=true), then wsl --shutdown" }
if ($DistroState -eq "ready")          { $Plan += "install git in $Distro if missing, clone $RepoUrl into ~/dendra-network there" }
if (-not $NoKeepAlive -and $KeepAliveState -ne "registered") { $Plan += "after install.sh, unless it refuses this PC: register the logon task '$TaskName' (keeps the WSL VM alive while you are logged on)" }
$flags = "--yes"
if ($Light) { $flags += " --light" }
if ($Validator) { $flags += " --validator" } elseif ($Miner) { $flags += " --miner" }
# Checked above: letters and digits only, so it can sit in the command string.
if ($PayoutAddress) { $flags += " --payout-address " + $PayoutAddress }
if ($Gpus) {
  $flags += " --gpus " + $Gpus
  Warn "-Gpus: one miner identity per card, each with its own stake, faucet drip and 24-word phrase. Under WSL 2 this is not measured on real multi-card hardware: the hourly pinning check (H6 of miner_health.sh) says whether each card held."
}
if ($DistroState -eq "ready") { $Plan += ("run inside {0}: CONFIG_URL={1} bash dendra-network/deploy/install.sh {2}  (disk read on {3}:)" -f $Distro, $ConfigUrl, $flags, $DriveLetter) }
if ($Driver -eq "missing") { Warn "no nvidia-smi.exe on Windows: without the NVIDIA Windows driver the GPU is invisible from WSL, and no mining model runs on the CPU: this PC then joins as a judge on the CPU if the WSL VM has the RAM deploy/hw_probe.sh asks for (MOE_CPU_MIN_RAM_MB), and is refused otherwise. Install the driver from nvidia.com first if this PC has an NVIDIA card." }
Say ""
Say "== [install] plan =="
foreach ($p in $Plan) { Say "  - $p" }
if (-not $Yes) {
  Say ""
  Say "  Nothing was done. Re-run with -Yes to apply the plan above."
  Resume "planned"; exit 2
}

# ---------------------------------------------------------------- 3. apply, stopping where a human is needed
function Step([string]$m) { Say ""; Say "== [install] $m ==" }
function Fail([string]$m) { Say "  [FAILED] $m"; Resume "failed"; exit 1 }

if ($WslState -eq "missing") {
  Step "installing WSL and $Distro"
  & $Wsl --install -d $Distro --no-launch
  if ($LASTEXITCODE -ne 0) { Fail "wsl --install returned $LASTEXITCODE. If it hangs at 0%, try: wsl --install --web-download -d $Distro" }
  Say ""
  Say "  WSL is installed. RESTART Windows now, then run this file again: it continues from here."
  Resume "restart_required"; exit 0
}
if ($WslState -eq "inbox") {
  Step "updating WSL to the Store build"
  & $Wsl --update
  if ($LASTEXITCODE -ne 0) { Fail "wsl --update returned $LASTEXITCODE. Install 'Windows Subsystem for Linux' from the Microsoft Store, then run this file again." }
  & $Wsl --shutdown
  & $Wsl --version *> $null
  if ($LASTEXITCODE -ne 0) { Fail "WSL still answers as the inbox build after the update. Install it from the Microsoft Store, then run this file again." }
  Say ""
  Say "  WSL is now the Store build. Run this file again: it re-reads the distribution with it."
  $WslState = "present"
  Resume "rerun_required"; exit 0
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
  Say "  account the miner runs under. Close it when the prompt appears, then run this file again."
  Resume "first_launch_required"; exit 0
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
if ! command -v git >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq git ca-certificates curl
fi
'@
if ($rc -ne 0) { Fail "installing git in $Distro" }
$clone = @'
set -e
if [ -d "$HOME/dendra-network/.git" ]; then
  echo "clone already present: $HOME/dendra-network"
else
  git clone REPO_URL "$HOME/dendra-network"
fi
'@
$clone = $clone.Replace("REPO_URL", $RepoUrl)
$rc = WslSh $null $clone
if ($rc -ne 0) { Fail "git clone $RepoUrl in $Distro" }

Step "handing over to deploy/install.sh inside $Distro"
Say "  from here on the output is install.sh's, then join.sh's. sudo inside $Distro may ask for your Linux password."
Say ""
# CONFIG_URL and the drive to measure cross into the distribution as ENVIRONMENT variables (WSLENV),
# never on the command line: an argument under `--` is read once more by the login shell before
# bash -lc sees it, and a URL with a query string would be split there. The command below carries no
# variable and no quote. The drive is the one read above, so both files judge the same disk.
$env:CONFIG_URL = $ConfigUrl
$env:DENDRA_WSL_HOST_MOUNT = "/mnt/" + $DriveLetter.ToLower()
$share = "CONFIG_URL/u:DENDRA_WSL_HOST_MOUNT/u"
if ($env:WSLENV) { $env:WSLENV = $env:WSLENV + ":" + $share } else { $env:WSLENV = $share }
$cmd = "bash dendra-network/deploy/install.sh " + $flags
& $Wsl -d $Distro --cd "~" -- bash -lc $cmd
$rc = $LASTEXITCODE

# THE LOGON TASK COMES AFTER THE ROLE. It used to be registered BEFORE install.sh, which is where the role of this
# PC is decided (deploy/hw_probe.sh --role): a PC refused there -- no usable NVIDIA card and a WSL VM below
# MOE_CPU_MIN_RAM_MB -- kept a hidden task holding its VM up at every logon, for no role at all. A refusal (exit 2)
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
# A task that could not be registered after a SUCCESSFUL install is a failed step (exit 1); after another outcome,
# that outcome's own code is kept, and the line above says the task is missing too.
if ($rc -eq 0 -and $TaskFailed) { Resume "failed"; exit 1 }

# HEALTH, FROM WINDOWS. The hourly check runs INSIDE the distribution and writes its alert file there: nothing on
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
  "  HEALTH, FROM WINDOWS (the hourly check runs inside $Distro, and nothing on the Windows desktop shows it):",
  ("    wsl -d {0} --cd ~ -- bash -lc 'bash {1}'" -f $Distro, $HealthScript),
  ("    its alert file exists only while something is wrong: ~/{0} inside {1}" -f $HealthAlert, $Distro),
  ("    (in File Explorer: \\wsl.localhost\{0}, then your Linux home folder)" -f $Distro)
)

if ($rc -ne 0) {
  Say ""
  # install.sh answers with the same three codes as this file; they are passed on, not folded into 1.
  switch ($rc) {
    2 { Say "  install.sh REFUSED (exit 2): read its reason above. The refused step changed nothing, and this run registered no logon task." }
    3 { Say "  install.sh could NOT MEASURE (exit 3): the host, or the miner's health once join.sh started it (it keeps running and registering). Read the reason above." }
    default { Say "  install.sh exited with ${rc}: a step failed. Windows is prepared; read the output above, then re-run just it:"; Say ("    wsl -d {0} --cd ~ -- bash -lc 'CONFIG_URL={1} DENDRA_WSL_HOST_MOUNT={2} {3}'" -f $Distro, $ConfigUrl, $env:DENDRA_WSL_HOST_MOUNT, $cmd) }
  }
  if ($rc -ne 2) { $HealthLines | ForEach-Object { Say $_ } }
  Resume ("linux_side_exit_" + $rc); exit $rc
}
$HealthLines | ForEach-Object { Say $_ }
Resume "joined"
exit 0
