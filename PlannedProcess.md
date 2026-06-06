# ReEncode Scanning Procces Plan

This document captures How the scanning process should go

## 1) How it should 

### Step 1: User stats scan
- User clicks Scan in Sources panel
- App gathers selected folders and emits the scan request.
- Main window starts a new scan token, resets previous state, clears all media tables, and disables scan controls.

### Step 2: Discovery thread walks filesystem
- Scanner thread recursively walks selected folders.
- Each file extension is matched against media type sets:
	- Images
	- Videos
	- Audio
- For each recognized media file, scanner emits `(media_type, absolute_path)`.
- For each scanner emit there is a record stored localy (first just the media_type, absolute_path).
- After the scan if there are local files that were not in scan they need to be removed from local storage (as they were renamed, removed, etc..)
- Scanner emits total discovered count when done. 
- The Initial row receations are done and only need to be updated after this.

### Step 3: Metadata thread enriches found files
- Every discovered file is queued to metadata worker. If it has been modified sinds last scan
- Metadata worker reads:
	- file size (`st_size`)
	- modified timestamp (`st_mtime`)
- If the file has been modified sinds last scan it should update the probe
    - runs ffprobe and parses JSON
    - update record with last scan and encoding, st_size
- When the info is complete the item is added to que to update record in the table.

## UI 
- When scanning is in progress all table actions are disabled until completed
- Every 100ms the records and status are updated
- When going trough the Table only the visible records are update (this is important as the list can become extreemly large. so this needs some smart rendering)

## Local Storage
- list per added folder 
    - Lists each media found in this folder
        - absolute_path (Key)
        - media_type
        - file_size
        - last_modified
        - encoding
        - last_scanned

- If the absolute path has not been found in the current  scan (step 1) it should be remove
- If the last_scanned is empty or modified since it should redo the scan

## Worker
### Goal
Define a reliable worker model for scanning that is fast, cancellable, and safe against stale async updates.

### Architecture
- ScanCoordinator runs on main thread.
- DiscoveryWorker runs in background and only discovers files.
- MetadataProbeWorker runs in background and handles metadata, probe decisions, and storage updates.
- UI never gets updated directly by workers; coordinator buffers and applies updates every 100 ms.

### ScanCoordinator Responsibilities
- Create new scan_id on each scan start.
- Reset UI state, clear tables, and disable actions for scan duration.
- Start/stop workers and own cancellation flag.
- Receive all worker signals and drop events where scan_id is stale.
- Buffer row updates and flush in batches every 100 ms.
- Transition scan state and finalize cleanup when both workers complete.

### DiscoveryWorker Responsibilities
- Walk selected folders recursively.
- Match extension to media type.
- Emit found file events with media_type and absolute_path.
- Emit discovery progress and discovery completed.
- Check cancellation frequently and exit cleanly.

### MetadataProbeWorker Responsibilities
- Consume discovered files from queue.
- Read file metadata (size, last_modified).
- Compare with local storage record.
- If unchanged: reuse stored probe/encoding fields.
- If changed or missing: run ffprobe for applicable media - - types and refresh encoding fields.
- Upsert storage record with latest values and last_scanned.
- Emit row_ready or failed_item events.
- Emit metadata/probe progress and completed.

### Queue and Update Rules
vDiscoveryWorker pushes found files into thread-safe queue for MetadataProbeWorker.
- Coordinator applies table updates every 100 ms.
- Coordinator applies bounded max rows per tick to keep UI responsive.
- Visible rows are prioritized for paint/update where possible.
- Status line updates every 100 ms from latest worker status.

### Signal Contract
- file_found(scan_id, media_type, absolute_path)
- row_ready(scan_id, media_type, absolute_path, file_size, last_modified, encoding, estimate, recommend)
- failed_item(scan_id, media_type, absolute_path, reason, phase)
- progress(scan_id, phase, completed, total)
- completed(scan_id, phase, cancelled)
- fatal_error(scan_id, phase, message)

### Cancellation and Stale Event Safety
- All worker events must include scan_id.
- Coordinator ignores events where event scan_id != active scan_id.
- Cancel sets cancellation flag and requests worker stop.
- Workers must emit completed(cancelled=true) before shutdown.
- UI is re-enabled only after coordinator confirms active scan fully finalized.

### Local Storage Lifecycle
- Primary key: absolute_path.
- Required fields: media_type, file_size, last_modified, encoding, last_scanned.
- On success: upsert record and set last_scanned to current scan_id or scan timestamp.
- Post-scan prune: delete records in scanned source scope not touched in current scan.
- Probe reuse rule: unchanged file_size + last_modified means probe/encoding can be reused.

### Failure Handling
- Non-fatal failures go to Failed table with reason and phase.
- Failure examples: stat failed, probe failed, storage write failed.
- Scan continues on per-item failure.
- Fatal worker failures stop scan and show global error summary.

### Completion Criteria
- Discovery completed.
- MetadataProbe queue drained.
- Pending UI buffer flushed.
- Prune pass done for current source scope.
- State returns to idle.

### State Machine (Draft Alignment)
- idle -> quickscan -> metadata -> idle
converting remains separate from scan state machine
- idle -> converting -> idle

## Failure
- When items fail to be scanned, the items should be moved to tab 'Failed' with the reason why it failed.
- These failures can be failing to get metadata, failing to get encoding(if applicable)


## Tables
The tables Images, Videos and Audio have the following columns:
Name, Size, Codec, Recommend, Estimate, Modified

The table Failed will contain the Columns:
Name, reason failure, obsolute Path

## StateMachine States
- idle
    - can start scan or convert
- quickscan
- metadata
    - can only go from quickscan
- converting
