<script>
	import { onMount, onDestroy } from 'svelte';

	let activeTab = 'convert';
	let selectedFiles = [];
	let isDragging = false;
	let queue = [];
	let history = [];
	let eventSource = null;

	// Upload progress tracking
	let uploadingFiles = []; // { file, progress, status: 'pending'|'uploading'|'complete'|'error' }
	let isUploading = false;

	// Toast notification
	let toastMessage = '';
	let toastVisible = false;
	let toastTimeout = null;

	// Settings (persisted)
	let settings = {
		outputMarkdown: true,
		outputJson: false,
		outputImages: true,
		forceOcr: false,
		paginateOutput: false
	};

	onMount(async () => {
		// Load settings
		try {
			const res = await fetch('/api/settings');
			if (res.ok) {
				const data = await res.json();
				settings = { ...settings, ...data };
			}
		} catch (e) {
			console.log('Using default settings');
		}

		// Load queue and history
		await loadQueue();
		await loadHistory();

		// Start listening to queue updates
		connectQueueSSE();
	});

	onDestroy(() => {
		if (eventSource) {
			eventSource.close();
		}
	});

	async function loadQueue() {
		try {
			const res = await fetch('/api/queue');
			if (res.ok) {
				queue = await res.json();
			}
		} catch (e) {
			console.error('Failed to load queue:', e);
		}
	}

	async function loadHistory() {
		try {
			const res = await fetch('/api/history');
			if (res.ok) {
				history = await res.json();
			}
		} catch (e) {
			console.error('Failed to load history:', e);
		}
	}

	function connectQueueSSE() {
		if (eventSource) {
			eventSource.close();
		}

		eventSource = new EventSource('/api/queue/stream');

		eventSource.onmessage = (event) => {
			const data = JSON.parse(event.data);

			if (data.type === 'queue_update') {
				queue = data.queue;
			} else if (data.type === 'job_complete') {
				loadHistory();
				loadQueue();
			}
		};

		eventSource.onerror = () => {
			// Reconnect after 2 seconds
			setTimeout(() => {
				if (eventSource) {
					eventSource.close();
				}
				connectQueueSSE();
			}, 2000);
		};
	}

	async function saveSettings() {
		try {
			await fetch('/api/settings', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(settings)
			});
		} catch (e) {
			console.error('Failed to save settings:', e);
		}
	}

	function handleDrop(e) {
		e.preventDefault();
		isDragging = false;
		const droppedFiles = Array.from(e.dataTransfer.files).filter(
			f => f.type === 'application/pdf' || f.name.endsWith('.pdf')
		);
		if (droppedFiles.length > 0) {
			selectedFiles = [...selectedFiles, ...droppedFiles];
		}
	}

	function handleFileSelect(e) {
		const newFiles = Array.from(e.target.files);
		selectedFiles = [...selectedFiles, ...newFiles];
		e.target.value = ''; // Reset input so same file can be added again
	}

	function removeFile(index) {
		selectedFiles = selectedFiles.filter((_, i) => i !== index);
	}

	function showToast(message, duration = 4000) {
		if (toastTimeout) {
			clearTimeout(toastTimeout);
		}
		toastMessage = message;
		toastVisible = true;
		toastTimeout = setTimeout(() => {
			toastVisible = false;
			toastMessage = '';
		}, duration);
	}

	function uploadFileWithProgress(file, index) {
		return new Promise((resolve, reject) => {
			const xhr = new XMLHttpRequest();
			const formData = new FormData();
			formData.append('file', file);
			formData.append('output_markdown', settings.outputMarkdown);
			formData.append('output_json', settings.outputJson);
			formData.append('output_images', settings.outputImages);
			formData.append('force_ocr', settings.forceOcr);
			formData.append('paginate_output', settings.paginateOutput);

			xhr.upload.onprogress = (event) => {
				if (event.lengthComputable) {
					const percent = Math.round((event.loaded / event.total) * 100);
					uploadingFiles = uploadingFiles.map((f, i) =>
						i === index ? { ...f, progress: percent, status: 'uploading' } : f
					);
				}
			};

			xhr.onload = () => {
				if (xhr.status >= 200 && xhr.status < 300) {
					uploadingFiles = uploadingFiles.map((f, i) =>
						i === index ? { ...f, progress: 100, status: 'complete' } : f
					);
					resolve();
				} else {
					uploadingFiles = uploadingFiles.map((f, i) =>
						i === index ? { ...f, status: 'error' } : f
					);
					reject(new Error(`Upload failed: ${xhr.status}`));
				}
			};

			xhr.onerror = () => {
				uploadingFiles = uploadingFiles.map((f, i) =>
					i === index ? { ...f, status: 'error' } : f
				);
				reject(new Error('Network error'));
			};

			xhr.open('POST', '/api/queue/add');
			xhr.send(formData);
		});
	}

	async function addToQueue() {
		if (selectedFiles.length === 0) return;

		// Save settings first
		await saveSettings();

		// Initialize upload tracking
		isUploading = true;
		uploadingFiles = selectedFiles.map(file => ({
			file,
			progress: 0,
			status: 'pending'
		}));

		const fileCount = selectedFiles.length;
		let successCount = 0;

		// Upload files sequentially with progress tracking
		for (let i = 0; i < uploadingFiles.length; i++) {
			try {
				await uploadFileWithProgress(uploadingFiles[i].file, i);
				successCount++;
			} catch (e) {
				console.error('Failed to upload:', uploadingFiles[i].file.name, e);
			}
		}

		// Clear state and show notification
		selectedFiles = [];
		uploadingFiles = [];
		isUploading = false;

		// Show toast notification - stay on Convert tab
		if (successCount > 0) {
			showToast(`${successCount} file${successCount > 1 ? 's' : ''} added to queue`);
		}

		await loadQueue();
	}

	async function removeFromQueue(jobId) {
		try {
			await fetch(`/api/queue/${jobId}`, { method: 'DELETE' });
			await loadQueue();
		} catch (e) {
			console.error('Failed to remove from queue:', e);
		}
	}

	function formatDate(dateStr) {
		const date = new Date(dateStr);
		return date.toLocaleString();
	}

	function formatFileSize(bytes) {
		if (bytes < 1024) return bytes + ' B';
		if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
		return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
	}

	function getStatusColor(status) {
		switch (status) {
			case 'complete': return 'var(--success)';
			case 'error': return 'var(--error)';
			case 'processing': return 'var(--warning)';
			case 'queued': return 'var(--text-secondary)';
			default: return 'var(--text-secondary)';
		}
	}

	function getProgressMessage(job) {
		if (job.status === 'queued') return 'Waiting in queue...';
		if (job.status === 'processing') {
			if (job.total_chunks > 1) {
				return `Processing chunk ${job.current_chunk}/${job.total_chunks}: ${job.message || ''}`;
			}
			return job.message || 'Processing...';
		}
		if (job.status === 'merging') return 'Merging chunks...';
		return job.message || '';
	}
</script>

<main>
	<header>
		<h1>Marker</h1>
		<p>Convert PDFs to Markdown</p>
	</header>

	<div class="tabs">
		<button class="tab" class:active={activeTab === 'convert'} onclick={() => activeTab = 'convert'}>
			Convert
		</button>
		<button class="tab" class:active={activeTab === 'queue'} onclick={() => activeTab = 'queue'}>
			Queue {#if queue.length > 0}<span class="badge">{queue.length}</span>{/if}
		</button>
		<button class="tab" class:active={activeTab === 'history'} onclick={() => activeTab = 'history'}>
			History
		</button>
	</div>

	{#if activeTab === 'convert'}
		<div class="convert-section">
			<!-- Upload Area -->
			{#if isUploading}
				<!-- Show upload progress -->
				<div class="upload-area card uploading">
					<div class="selected-files">
						<p class="selected-count">Uploading {uploadingFiles.length} file{uploadingFiles.length > 1 ? 's' : ''}...</p>
						<div class="file-list">
							{#each uploadingFiles as upload, index}
								<div class="file-item uploading-item">
									<span class="file-icon">
										{#if upload.status === 'complete'}
											&#10003;
										{:else if upload.status === 'error'}
											&#10007;
										{:else if upload.status === 'uploading'}
											&#8635;
										{:else}
											&#128196;
										{/if}
									</span>
									<div class="file-info">
										<span class="file-name">{upload.file.name}</span>
										<div class="upload-progress-bar">
											<div
												class="upload-progress-fill"
												class:complete={upload.status === 'complete'}
												class:error={upload.status === 'error'}
												style="width: {upload.progress}%"
											></div>
										</div>
									</div>
									<span class="upload-percent">
										{#if upload.status === 'complete'}
											Done
										{:else if upload.status === 'error'}
											Error
										{:else if upload.status === 'pending'}
											Waiting
										{:else}
											{upload.progress}%
										{/if}
									</span>
								</div>
							{/each}
						</div>
					</div>
				</div>
			{:else}
				<div
					class="upload-area card"
					class:dragging={isDragging}
					class:has-file={selectedFiles.length > 0}
					ondragover={(e) => { e.preventDefault(); isDragging = true; }}
					ondragleave={() => isDragging = false}
					ondrop={handleDrop}
					onclick={() => document.getElementById('file-input').click()}
					role="button"
					tabindex="0"
					onkeydown={(e) => e.key === 'Enter' && document.getElementById('file-input').click()}
				>
					<input
						type="file"
						id="file-input"
						accept=".pdf,application/pdf"
						multiple
						onchange={handleFileSelect}
					/>

					{#if selectedFiles.length > 0}
						<div class="selected-files" onclick={(e) => e.stopPropagation()}>
							<p class="selected-count">{selectedFiles.length} file{selectedFiles.length > 1 ? 's' : ''} selected</p>
							<div class="file-list">
								{#each selectedFiles as file, index}
									<div class="file-item">
										<span class="file-icon">&#128196;</span>
										<span class="file-name">{file.name}</span>
										<span class="file-size">{formatFileSize(file.size)}</span>
										<button class="remove-btn" onclick={() => removeFile(index)}>&#10005;</button>
									</div>
								{/each}
							</div>
							<p class="add-more">Click or drop to add more files</p>
						</div>
					{:else}
						<div class="upload-prompt">
							<span class="upload-icon">&#128194;</span>
							<p>Drop PDFs here or click to browse</p>
							<p class="upload-hint">You can select multiple files</p>
						</div>
					{/if}
				</div>
			{/if}

			<!-- Output Options -->
			<div class="options card">
				<h3>Output Options</h3>
				<div class="checkbox-group">
					<label class="checkbox-item">
						<input type="checkbox" bind:checked={settings.outputMarkdown} />
						<span>Markdown</span>
					</label>
					<label class="checkbox-item">
						<input type="checkbox" bind:checked={settings.outputJson} />
						<span>JSON</span>
					</label>
					<label class="checkbox-item">
						<input type="checkbox" bind:checked={settings.outputImages} />
						<span>Images</span>
					</label>
				</div>

				<h3 style="margin-top: 20px;">Processing Options</h3>
				<div class="checkbox-group">
					<label class="checkbox-item">
						<input type="checkbox" bind:checked={settings.forceOcr} />
						<span>Force OCR</span>
					</label>
					<label class="checkbox-item">
						<input type="checkbox" bind:checked={settings.paginateOutput} />
						<span>Paginate Output</span>
					</label>
				</div>
			</div>

			<!-- Add to Queue Button -->
			<button
				class="convert-btn primary"
				onclick={addToQueue}
				disabled={selectedFiles.length === 0 || isUploading}
			>
				{#if isUploading}
					Uploading...
				{:else}
					Add to Queue ({selectedFiles.length} file{selectedFiles.length !== 1 ? 's' : ''})
				{/if}
			</button>
		</div>

	{:else if activeTab === 'queue'}
		<!-- Queue Tab -->
		<div class="queue-section">
			{#if queue.length === 0}
				<div class="empty-state card">
					<p>Queue is empty</p>
					<p class="empty-hint">Add files from the Convert tab</p>
				</div>
			{:else}
				<div class="queue-list">
					{#each queue as job, index}
						<div class="queue-item card" class:processing={job.status === 'processing'}>
							<div class="queue-position">
								{#if job.status === 'processing'}
									<span class="processing-indicator"></span>
								{:else}
									<span class="position-number">{index + 1}</span>
								{/if}
							</div>
							<div class="queue-content">
								<div class="queue-main">
									<span class="queue-filename">{job.filename}</span>
									<span class="queue-size">{formatFileSize(job.file_size)}</span>
								</div>
								<div class="queue-progress">
									<span class="queue-status" style="color: {getStatusColor(job.status)}">
										{job.status === 'processing' ? 'Processing' : 'Queued'}
									</span>
									<span class="queue-message">{getProgressMessage(job)}</span>
								</div>
								{#if job.status === 'processing'}
									<div class="progress-bar">
										<div class="progress-fill" style="width: {job.percent || 0}%"></div>
									</div>
								{/if}
							</div>
							{#if job.status === 'queued'}
								<button class="remove-queue-btn" onclick={() => removeFromQueue(job.id)}>
									&#10005;
								</button>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
		</div>

	{:else}
		<!-- History Tab -->
		<div class="history-section">
			{#if history.length === 0}
				<div class="empty-state card">
					<p>No conversions yet</p>
				</div>
			{:else}
				<div class="history-list">
					{#each history as job}
						<div class="history-item card">
							<div class="history-main">
								<span class="history-filename">{job.filename}</span>
								<span class="history-date">{formatDate(job.created_at)}</span>
							</div>
							<div class="history-meta">
								<span class="history-status" style="color: {getStatusColor(job.status)}">
									{job.status}
								</span>
								{#if job.status === 'complete'}
									<div class="history-downloads">
										{#if job.has_markdown}
											<a href="/api/download/{job.id}/markdown" class="download-btn">
												&#128196; MD
											</a>
										{/if}
										{#if job.has_json}
											<a href="/api/download/{job.id}/json" class="download-btn">
												&#123;&#125; JSON
											</a>
										{/if}
										{#if job.has_images}
											<a href="/api/download/{job.id}/images" class="download-btn">
												&#128247; Images
											</a>
										{/if}
									</div>
								{:else if job.status === 'error'}
									<span class="error-message">{job.error_message || 'Unknown error'}</span>
								{/if}
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{/if}

	<!-- Toast Notification -->
	{#if toastVisible}
		<div class="toast" class:visible={toastVisible}>
			<span class="toast-icon">&#10003;</span>
			{toastMessage}
		</div>
	{/if}
</main>

<style>
	main {
		max-width: 800px;
		margin: 0 auto;
		padding: 40px 20px;
	}

	header {
		text-align: center;
		margin-bottom: 40px;
	}

	header h1 {
		font-size: 2.5rem;
		margin-bottom: 8px;
	}

	header p {
		color: var(--text-secondary);
	}

	.badge {
		background: var(--accent);
		color: white;
		font-size: 12px;
		padding: 2px 8px;
		border-radius: 10px;
		margin-left: 6px;
	}

	.upload-area {
		padding: 40px;
		text-align: center;
		cursor: pointer;
		transition: all 0.2s;
		border: 2px dashed var(--border);
		background: var(--bg-secondary);
	}

	.upload-area:hover,
	.upload-area.dragging {
		border-color: var(--accent);
		background: rgba(59, 130, 246, 0.1);
	}

	.upload-area.has-file {
		border-style: solid;
		border-color: var(--success);
		cursor: default;
	}

	.upload-icon {
		font-size: 48px;
		display: block;
		margin-bottom: 16px;
	}

	.upload-hint {
		color: var(--text-secondary);
		font-size: 14px;
		margin-top: 8px;
	}

	.selected-files {
		text-align: left;
	}

	.selected-count {
		font-weight: 600;
		margin-bottom: 12px;
		text-align: center;
	}

	.file-list {
		max-height: 200px;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.file-item {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 12px;
		background: var(--bg-tertiary);
		border-radius: 6px;
	}

	.file-item .file-icon {
		font-size: 20px;
	}

	.file-item .file-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.file-item .file-size {
		color: var(--text-secondary);
		font-size: 13px;
	}

	.remove-btn {
		background: none;
		border: none;
		color: var(--text-secondary);
		padding: 4px 8px;
		cursor: pointer;
		font-size: 14px;
	}

	.remove-btn:hover {
		color: var(--error);
	}

	.add-more {
		text-align: center;
		color: var(--text-secondary);
		font-size: 13px;
		margin-top: 12px;
		cursor: pointer;
	}

	.options {
		margin-top: 20px;
	}

	.options h3 {
		font-size: 14px;
		color: var(--text-secondary);
		margin-bottom: 12px;
		text-transform: uppercase;
		letter-spacing: 0.5px;
	}

	.convert-btn {
		margin-top: 20px;
		width: 100%;
		padding: 16px;
		font-size: 16px;
	}

	/* Queue styles */
	.queue-list {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.queue-item {
		display: flex;
		align-items: flex-start;
		gap: 16px;
		padding: 16px 20px;
	}

	.queue-item.processing {
		border-color: var(--warning);
	}

	.queue-position {
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
	}

	.position-number {
		background: var(--bg-tertiary);
		width: 28px;
		height: 28px;
		border-radius: 50%;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 14px;
		color: var(--text-secondary);
	}

	.processing-indicator {
		width: 12px;
		height: 12px;
		background: var(--warning);
		border-radius: 50%;
		animation: pulse 1.5s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; transform: scale(1); }
		50% { opacity: 0.5; transform: scale(1.1); }
	}

	.queue-content {
		flex: 1;
		min-width: 0;
	}

	.queue-main {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 4px;
	}

	.queue-filename {
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.queue-size {
		color: var(--text-secondary);
		font-size: 13px;
		flex-shrink: 0;
		margin-left: 12px;
	}

	.queue-progress {
		display: flex;
		gap: 8px;
		font-size: 14px;
		margin-bottom: 8px;
	}

	.queue-status {
		font-weight: 500;
	}

	.queue-message {
		color: var(--text-secondary);
	}

	.progress-bar {
		height: 6px;
		background: var(--bg-tertiary);
		border-radius: 3px;
		overflow: hidden;
	}

	.progress-fill {
		height: 100%;
		background: var(--accent);
		transition: width 0.3s ease;
	}

	.remove-queue-btn {
		background: none;
		border: none;
		color: var(--text-secondary);
		padding: 8px;
		cursor: pointer;
		font-size: 16px;
	}

	.remove-queue-btn:hover {
		color: var(--error);
	}

	/* History styles */
	.history-list {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.history-item {
		padding: 16px 20px;
	}

	.history-main {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 8px;
	}

	.history-filename {
		font-weight: 500;
	}

	.history-date {
		color: var(--text-secondary);
		font-size: 14px;
	}

	.history-meta {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.history-status {
		font-size: 14px;
		text-transform: capitalize;
	}

	.history-downloads {
		display: flex;
		gap: 8px;
	}

	.download-btn {
		padding: 6px 12px;
		background: var(--bg-tertiary);
		border-radius: 4px;
		color: var(--text-primary);
		text-decoration: none;
		font-size: 13px;
		transition: background 0.2s;
	}

	.download-btn:hover {
		background: var(--border);
	}

	.error-message {
		color: var(--error);
		font-size: 13px;
	}

	.empty-state {
		text-align: center;
		padding: 60px;
		color: var(--text-secondary);
	}

	.empty-hint {
		font-size: 14px;
		margin-top: 8px;
	}

	/* Upload progress styles */
	.upload-area.uploading {
		border-style: solid;
		border-color: var(--accent);
		cursor: default;
	}

	.uploading-item {
		flex-wrap: wrap;
	}

	.uploading-item .file-icon {
		width: 24px;
		text-align: center;
	}

	.uploading-item .file-info {
		flex: 1;
		min-width: 0;
	}

	.uploading-item .file-name {
		display: block;
		margin-bottom: 6px;
	}

	.upload-progress-bar {
		height: 4px;
		background: var(--bg-tertiary);
		border-radius: 2px;
		overflow: hidden;
	}

	.upload-progress-fill {
		height: 100%;
		background: var(--accent);
		transition: width 0.15s ease;
	}

	.upload-progress-fill.complete {
		background: var(--success);
	}

	.upload-progress-fill.error {
		background: var(--error);
	}

	.upload-percent {
		font-size: 12px;
		color: var(--text-secondary);
		width: 50px;
		text-align: right;
	}

	/* Toast notification */
	.toast {
		position: fixed;
		bottom: 24px;
		left: 50%;
		transform: translateX(-50%) translateY(100px);
		background: var(--success);
		color: white;
		padding: 12px 24px;
		border-radius: 8px;
		font-weight: 500;
		display: flex;
		align-items: center;
		gap: 8px;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
		opacity: 0;
		transition: transform 0.3s ease, opacity 0.3s ease;
		z-index: 1000;
	}

	.toast.visible {
		transform: translateX(-50%) translateY(0);
		opacity: 1;
	}

	.toast-icon {
		font-size: 18px;
	}
</style>
