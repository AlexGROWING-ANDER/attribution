param([Parameter(Mandatory=$true)][ValidateSet('checkpoint','step','global','check')][string]$Mode,[string]$Json)
$ErrorActionPreference='Stop'
$work=$PSScriptRoot
$encoding=[System.Text.UTF8Encoding]::new($false)
$checkpointPath=Join-Path $work 'checkpoint_plan.json'
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
function Get-IoEvents {
 $path=Join-Path $work 'io_events.jsonl'
 if(-not (Test-Path $path)){return @()}
 return @(Get-Content -LiteralPath $path -Encoding UTF8 | ForEach-Object {$_|ConvertFrom-Json})
}
function Assert-CheckpointPlan($value,$index) {
 Require-Fields $value @('schema','case_id','basis','checkpoints')
 if($value.schema -cne 'case-checkpoint-plan-v1'){throw 'Invalid checkpoint schema id'}
 $identity=$index.identity
 if($null -eq $identity -or [string]$identity.id -cne [string]$value.case_id){throw 'Checkpoint plan case_id does not match the current case'}
 $basis=@($value.basis)
 if($basis.Count -lt 1 -or $basis.Count -gt 3){throw 'Checkpoint basis must contain 1 to 3 items'}
 if(@($basis|Select-Object -Unique).Count -ne $basis.Count){throw 'Checkpoint basis must be unique'}
 foreach($item in $basis){if(@('instruction','identity','task_policy') -cnotcontains $item){throw 'Invalid checkpoint basis'}}
 $checkpoints=@($value.checkpoints)
 if($checkpoints.Count -lt 1 -or $checkpoints.Count -gt 8){throw 'Checkpoint count must be between 1 and 8'}
 $ids=@();$dependencies=@{};$requiredCount=0
 foreach($checkpoint in $checkpoints) {
  Require-Fields $checkpoint @('id','type','object','requirement','required','depends_on','acceptance','evidence_expectation')
  if([string]$checkpoint.id -cnotmatch '^CP[0-9]+$'){throw 'Checkpoint id must match CP[0-9]+'}
  if($ids -ccontains [string]$checkpoint.id){throw 'Checkpoint IDs must be unique'}
  if(@('goal','precondition','constraint','delivery') -cnotcontains $checkpoint.type){throw 'Invalid checkpoint type'}
  foreach($field in @('object','requirement','acceptance')) {if([string]::IsNullOrWhiteSpace([string]$checkpoint.$field)){throw ('Empty checkpoint '+$field)}}
  if($checkpoint.required -isnot [bool]){throw 'checkpoint required must be boolean'}
  if($checkpoint.required){$requiredCount++}
  $expect=@($checkpoint.evidence_expectation)
  if($expect.Count -lt 1){throw 'evidence_expectation must not be empty'}
  if(@($expect|Select-Object -Unique).Count -ne $expect.Count){throw 'evidence_expectation must be unique'}
  foreach($kind in $expect){if(@('screenshot','execution_result','module_output','metadata') -cnotcontains $kind){throw 'Invalid evidence_expectation'}}
  $deps=@($checkpoint.depends_on)
  if(@($deps|Select-Object -Unique).Count -ne $deps.Count){throw 'Checkpoint dependencies must be unique'}
  foreach($dependency in $deps){if([string]$dependency -cnotmatch '^CP[0-9]+$'){throw 'Invalid checkpoint dependency id'}}
  $ids+=[string]$checkpoint.id;$dependencies[[string]$checkpoint.id]=$deps
 }
 if($requiredCount -lt 1){throw 'At least one checkpoint must be required'}
 foreach($identifier in $ids) {foreach($dependency in @($dependencies[$identifier])) {if($ids -cnotcontains [string]$dependency){throw ('Checkpoint '+$identifier+' depends on unknown '+$dependency)}}}
 $remaining=@{};foreach($identifier in $ids){$remaining[$identifier]=@($dependencies[$identifier])}
 while($remaining.Count -gt 0) {
  $ready=@()
  foreach($identifier in @($remaining.Keys)) {
   $blocked=$false
   foreach($dependency in @($remaining[$identifier])) {if($remaining.ContainsKey([string]$dependency)){$blocked=$true;break}}
   if(-not $blocked){$ready+=$identifier}
  }
  if($ready.Count -eq 0){throw 'Checkpoint dependencies must not contain a cycle'}
  foreach($identifier in $ready){$remaining.Remove($identifier)}
 }
 $serialized=$value|ConvertTo-Json -Depth 80 -Compress
 foreach($source in @($index.sources)) {if($null -ne $source -and -not [string]::IsNullOrWhiteSpace([string]$source.source_id) -and $serialized.Contains([string]$source.source_id)){throw 'Checkpoint plan must not cite trajectory evidence IDs'}}
}
function Assert-CheckpointSubmission($index) {
 if(-not (Test-Path $checkpointPath)){throw 'Submit checkpoint_plan.json before trajectory analysis, step judgments or derived views'}
 $plan=Read-Json $checkpointPath
 Assert-CheckpointPlan $plan $index
 $accepted=@(Get-IoEvents | Where-Object {$_.mode -eq 'checkpoint' -and $_.accepted -eq $true})
 if($accepted.Count -ne 1){throw 'Exactly one accepted checkpoint submission is required'}
 $digest=(Get-FileHash -LiteralPath $checkpointPath -Algorithm SHA256).Hash.ToLowerInvariant()
 if([string]$accepted[0].detail -cne ('Saved checkpoint plan sha256='+$digest)){throw 'checkpoint_plan.json changed after its accepted submission'}
 return $plan
}
function Assert-GoalAlignment($plan,$global) {
 $goals=@($global.goals)
 foreach($checkpoint in @($plan.checkpoints)) {
  $matches=@($goals|Where-Object {$null -ne $_ -and [string]$_.id -ceq [string]$checkpoint.id})
  if($matches.Count -eq 0) {if($checkpoint.required){throw ('Required checkpoint '+$checkpoint.id+' is missing from final goals')};continue}
  if($matches.Count -gt 1){throw ('Checkpoint '+$checkpoint.id+' must map to exactly one final goal')}
  if([string]$matches[0].object -cne [string]$checkpoint.object -or [string]$matches[0].requirement -cne [string]$checkpoint.requirement){throw ('Goal '+$checkpoint.id+' must preserve the checkpoint object and requirement')}
 }
}
function Save-CheckpointPlan($index,$value) {
 if(Test-Path $checkpointPath){throw 'Checkpoint plan is already saved'}
 $accepted=@(Get-IoEvents|Where-Object {$_.accepted -eq $true -and @('checkpoint','step','global','check','derive') -ccontains $_.mode})
 if(@($accepted|Where-Object {$_.mode -eq 'checkpoint'}).Count -gt 0){throw 'Checkpoint plan was already submitted'}
 if($accepted.Count -gt 0){throw 'Checkpoint plan must be submitted before later helper work'}
 if(Test-Path (Join-Path $work 'global_summary.json')){throw 'Checkpoint plan must be submitted before global summary'}
 $stepsPath=Join-Path $work 'step_judgments'
 if((Test-Path $stepsPath) -and @(Get-ChildItem -LiteralPath $stepsPath -Filter '*.json' -File).Count -gt 0){throw 'Checkpoint plan must be submitted before step judgments'}
 $derivedPath=Join-Path $work 'derived_views'
 if((Test-Path $derivedPath) -and @(Get-ChildItem -LiteralPath $derivedPath -Recurse -File).Count -gt 0){throw 'Checkpoint plan must be submitted before derived views'}
 Assert-CheckpointPlan $value $index
 Save-Json $checkpointPath $value
 $digest=(Get-FileHash -LiteralPath $checkpointPath -Algorithm SHA256).Hash.ToLowerInvariant()
 Log-Event $true ('Saved checkpoint plan sha256='+$digest)
 @{saved='checkpoint_plan.json';sha256=$digest;checkpoints=@($value.checkpoints).Count}|ConvertTo-Json -Compress
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
 if($Mode -eq 'checkpoint') {
  if(-not $Json){throw 'Json payload required'}
  $value=$Json|ConvertFrom-Json
  Save-CheckpointPlan $index $value
 } elseif($Mode -eq 'step') {
  $null=Assert-CheckpointSubmission $index
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
  $plan=Assert-CheckpointSubmission $index
  if($pending.Count){throw ('Unfinished steps: '+($pending -join ','))}
  Assert-RevisionFiles $steps
  $known=@($index.sources|ForEach-Object {$_.source_id})
  foreach($step in $steps){$null=Read-CheckedStep ([int]$step) $known}
  $value=$Json|ConvertFrom-Json
  $schema=Read-Json (Join-Path $work 'global_summary.schema.json')
  Require-Fields $value @($schema.required)
  Assert-GoalAlignment $plan $value
  Save-Json (Join-Path $work 'global_summary.json') $value
  Log-Event $true 'Saved global semantic judgment'
  '{"saved":"global_summary.json"}'
 } else {
  $plan=Assert-CheckpointSubmission $index
  if($pending.Count){throw ('Unfinished steps: '+($pending -join ','))}
  Assert-RevisionFiles $steps
  $known=@($index.sources|ForEach-Object {$_.source_id})
  $global=Read-Json (Join-Path $work 'global_summary.json')
  Assert-GoalAlignment $plan $global
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
