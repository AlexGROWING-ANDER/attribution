param([Parameter(Mandatory=$true)][ValidateSet('step','global','check')][string]$Mode,[string]$Json)
$ErrorActionPreference='Stop'
$work=$PSScriptRoot
$encoding=[System.Text.UTF8Encoding]::new($false)
function Read-Json([string]$p) { return ([System.IO.File]::ReadAllText($p,[System.Text.Encoding]::UTF8)|ConvertFrom-Json) }
function Save-Json([string]$p,$value) { [System.IO.File]::WriteAllText($p,($value|ConvertTo-Json -Depth 80),$encoding) }
function Log-Event([bool]$ok,[string]$detail) {
 $row=@{at=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds();mode=$Mode;accepted=$ok;detail=$detail}|ConvertTo-Json -Compress
 [System.IO.File]::AppendAllText((Join-Path $work 'io_events.jsonl'),$row+"`n",$encoding)
}
function Require-Fields($value,$names) {
 $actual=@($value.PSObject.Properties.Name)
 foreach($name in $names) { if($actual -cnotcontains $name){throw "Missing field $name"} }
 foreach($name in $actual) { if($names -cnotcontains $name){throw "Unexpected field $name"} }
}
function References($value) {
 if($null -eq $value){return}
 if($value -is [System.Management.Automation.PSCustomObject]) {
  foreach($prop in $value.PSObject.Properties) {
   if($prop.Name -eq 'evidence_ids') { foreach($id in @($prop.Value)){Write-Output ([string]$id)} }
   else {References $prop.Value}
  }
 } elseif($value -is [System.Collections.IEnumerable] -and $value -isnot [string]) {foreach($v in $value){References $v}}
}
function Assert-RevisionFiles($steps) {
 $allowed=@($steps|ForEach-Object {'revision_{0:D3}.json' -f [int]$_})
 foreach($file in Get-ChildItem (Join-Path $work 'step_judgments') -Filter 'revision_*.json' -File) {
  if($allowed -cnotcontains $file.Name){throw ('Unexpected revision file: '+$file.Name)}
 }
}
function Read-CheckedStep([int]$step,$known) {
 $original=Read-Json (Join-Path $work ('step_judgments/step_{0:D3}.json' -f $step))
 $path=Join-Path $work ('step_judgments/revision_{0:D3}.json' -f $step)
 if(-not (Test-Path $path)){return $original}
 $revised=Read-Json $path
 Require-Fields $revised @('step','verdict','summary','evidence')
 if((($revised.step -isnot [int]) -and ($revised.step -isnot [long])) -or $revised.step -ne $step){throw ('Revision step mismatch: '+$step)}
 if($revised.verdict -cne $original.verdict){throw ('Step '+$step+' revision cannot change the original verdict; append observations and explain them in summary/limitations')}
 if($revised.summary -isnot [string] -or [string]::IsNullOrWhiteSpace($revised.summary)){throw 'Revision summary must be a nonempty string'}
 if($revised.evidence -isnot [array] -or @($revised.evidence).Count -eq 0){throw 'Revision evidence must be a nonempty JSON array'}
 $prior=@($original.evidence)
 if($revised.evidence.Count -lt $prior.Count){throw ('Step '+$step+' revision removed original evidence; restore the original prefix and append new observations')}
 for($j=0;$j -lt $revised.evidence.Count;$j++) {
  $ev=$revised.evidence[$j]
  Require-Fields $ev @('source_id','phase','locator','observation')
  foreach($field in @('source_id','phase','locator','observation')) {
   if($ev.$field -isnot [string] -or $ev.$field.Length -eq 0){throw ('Revision evidence '+($j+1)+' field '+$field+' must be a nonempty string')}
   if($j -lt $prior.Count -and $ev.$field -cne $prior[$j].$field) {
    throw ('Original evidence E{0:D2}_{1} differs at {2}. Restore the original evidence prefix unchanged; append the corrected observation as a new item and explain it in summary/limitations.' -f $step,($j+1),$field)
   }
  }
  if($known -cnotcontains $ev.source_id){throw 'Unknown revision source_id'}
  if(@('before_action','after_action','decision','execution','metadata','final_observation','unknown') -cnotcontains $ev.phase){throw 'Invalid revision phase'}
 }
 return $revised
}
try {
 $index=Read-Json (Join-Path $work 'input_index.json')
 $steps=@($index.available_steps|Sort-Object)
 $pending=@($steps|Where-Object {-not (Test-Path (Join-Path $work ('step_judgments/step_{0:D3}.json' -f [int]$_)))})
 if($Mode -eq 'step') {
  if(-not $Json){throw 'Json payload required'}
  $value=$Json|ConvertFrom-Json
  Require-Fields $value @('step','verdict','summary','evidence')
  if($pending.Count -eq 0){throw 'All steps are already saved'}
  if([int]$value.step -ne [int]$pending[0]){throw ('Current required step is '+$pending[0])}
  if(($value.step -isnot [int]) -and ($value.step -isnot [long])){throw 'step must be an integer'}
  if($value.evidence -isnot [array]){throw 'evidence must be a JSON array'}
  if(@('reasonable','error','uncertain') -cnotcontains $value.verdict){throw 'Invalid verdict'}
  if([string]::IsNullOrWhiteSpace([string]$value.summary)){throw 'Empty summary'}
  if(@($value.evidence).Count -eq 0){throw 'Evidence observations required'}
  $known=@($index.sources|ForEach-Object {$_.source_id})
  foreach($ev in @($value.evidence)) {
   Require-Fields $ev @('source_id','phase','locator','observation')
   if($known -cnotcontains $ev.source_id){throw 'Unknown source_id'}
   if(@('before_action','after_action','decision','execution','metadata','final_observation','unknown') -cnotcontains $ev.phase){throw 'Invalid phase'}
   if([string]::IsNullOrWhiteSpace([string]$ev.locator) -or [string]::IsNullOrWhiteSpace([string]$ev.observation)){throw 'Empty evidence observation'}
  }
  $target=Join-Path $work ('step_judgments/step_{0:D3}.json' -f [int]$value.step)
  Save-Json $target $value
  $ids=@(for($j=1;$j -le @($value.evidence).Count;$j++){ 'E{0:D2}_{1}' -f [int]$value.step,$j })
  Log-Event $true ('Saved step '+$value.step)
  @{saved=$value.step;evidence_ids=$ids}|ConvertTo-Json -Compress
 } elseif($Mode -eq 'global') {
  if($pending.Count){throw ('Unfinished steps: '+($pending -join ','))}
  Assert-RevisionFiles $steps
  $known=@($index.sources|ForEach-Object {$_.source_id})
  foreach($step in $steps){$null=Read-CheckedStep ([int]$step) $known}
  $value=$Json|ConvertFrom-Json
  $schema=Read-Json (Join-Path $work 'global_summary.schema.json')
  Require-Fields $value @($schema.required)
  Save-Json (Join-Path $work 'global_summary.json') $value
  Log-Event $true 'Saved global semantic judgment'
  '{"saved":"global_summary.json"}'
 } else {
  if($pending.Count){throw ('Unfinished steps: '+($pending -join ','))}
  Assert-RevisionFiles $steps
  $known=@($index.sources|ForEach-Object {$_.source_id})
  $global=Read-Json (Join-Path $work 'global_summary.json')
  $ids=@()
  foreach($step in $steps) {
   $value=Read-CheckedStep ([int]$step) $known
   for($j=1;$j -le @($value.evidence).Count;$j++){$ids+=('E{0:D2}_{1}' -f [int]$step,$j)}
  }
  $bad=@(References $global|Where-Object {$ids -cnotcontains $_}|Select-Object -Unique)
  if($bad.Count){throw ('Unknown evidence refs: '+($bad -join ','))}
  Log-Event $true 'Local JSON and reference check; final strict validator not invoked'
  @{ok=$true;steps=$steps.Count;evidence_ids=$ids.Count;strict_validation='caller_only'}|ConvertTo-Json -Compress
 }
} catch {Log-Event $false $_.Exception.Message;Write-Error $_.Exception.Message;exit 1}
