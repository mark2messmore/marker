<script>
	import { onMount } from 'svelte';

	let activeTab = 'convert';
	let files = [];
	let isDragging = false;
	let isConverting = false;
	let progress = { status: 'idle', message: '', percent: 0 };
	let history = [];
	let currentJobId = null;
	let eventSource = null;

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

		// Load history
		await loadHistory();
	});

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
			files = droppedFiles;
		}
	}

	function handleFileSelect(e) {
		files = Array.from(e.target.files);
	}

	function connectSSE(jobId) {
		if (eventSource) {
			eventSource.close();
		}

		eventSource = new EventSource(`/api/progress/${jobId}`);

		eventSource.onmessage = (event) => {
			const data = JSON.parse(event.data);
			progress = data;

			if (data.status === 'complete' || data.status === 'error') {
				eventSource.close();
				eventSource = null;
				isConverting = false;
				loadHistory();
			}
		};

		eventSource.onerror = () => {
			eventSource.close();
			eventSource = null;
			isConverting = false;
			progress = { status: 'error', message: 'Connection lost', percent: 0 };
		};
	}

	async function startConversion() {
		if (files.length === 0 || isConverting) return;

		isConverting = true;
		progress = { status: 'uploading', message: 'Uploading file...', percent: 0 };

		const formData = new FormData();
		formData.append('file', files[0]);
		formData.append('output_markdown', settings.outputMarkdown);
		formData.append('output_json', settings.outputJson);
		formData.append('output_images', settings.outputImages);
		formData.append('force_ocr', settings.forceOcr);
		formData.append('paginate_output', settings.paginateOutput);

		try {
			const res = await fetch('/api/convert', {
				method: 'POST',
				body: formData
			});

			if (res.ok) {
				const data = await res.json();
				currentJobId = data.job_id;
				progress = { status: 'processing', message: 'Starting conversion...', percent: 5 };
				connectSSE(currentJobId);
			} else {
				const error = await res.json();
				progress = { status: 'error', message: error.detail || 'Upload failed', percent: 0 };
				isConverting = false;
			}
		} catch (e) {
			progress = { status: 'error', message: 'Network error', percent: 0 };
			isConverting = false;
		}

		// Save settings after conversion starts
		saveSettings();
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
			default: return 'var(--text-secondary)';
		}
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
		<button class="tab" class:active={activeTab === 'history'} onclick={() => activeTab = 'history'}>
			History
		</button>
	</div>

	{#if activeTab === 'convert'}
		<div class="convert-section">
			<!-- Upload Area -->
			<div
				class="upload-area card"
				class:dragging={isDragging}
				class:has-file={files.length > 0}
				ondragover={(e) => { e.preventDefault(); isDragging = true; }}
				ondragleave={() => isDragging = false}
				ondrop={handleDrop}
				onclick={() => document.getElementById('file-input').click()}
				role="button"
				tabindex="0"
			>
				<input
					type="file"
					id="file-input"
					accept=".pdf,application/pdf"
					onchange={handleFileSelect}
				/>

				{#if files.length > 0}
					<div class="file-info">
						<span class="file-icon">&#128196;</span>
						<span class="file-name">{files[0].name}</span>
						<span class="file-size">{formatFileSize(files[0].size)}</span>
					</div>
				{:else}
					<div class="upload-prompt">
						<span class="upload-icon">&#128194;</span>
						<p>Drop PDF here or click to browse</p>
					</div>
				{/if}
			</div>

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

			<!-- Progress -->
			{#if isConverting || progress.status !== 'idle'}
				<div class="progress-section card">
					<div class="progress-header">
						<span class="progress-status" style="color: {getStatusColor(progress.status)}">
							{progress.status.charAt(0).toUpperCase() + progress.status.slice(1)}
						</span>
						<span class="progress-percent">{progress.percent}%</span>
					</div>
					<div class="progress-bar">
						<div class="progress-fill" style="width: {progress.percent}%"></div>
					</div>
					<p class="progress-message">{progress.message}</p>
				</div>
			{/if}

			<!-- Convert Button -->
			<button
				class="convert-btn primary"
				onclick={startConversion}
				disabled={files.length === 0 || isConverting}
			>
				{isConverting ? 'Converting...' : 'Convert'}
			</button>
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
								{/if}
							</div>
						</div>
					{/each}
				</div>
			{/if}
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

	.upload-area {
		padding: 60px 40px;
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
	}

	.upload-icon {
		font-size: 48px;
		display: block;
		margin-bottom: 16px;
	}

	.file-info {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 12px;
	}

	.file-icon {
		font-size: 32px;
	}

	.file-name {
		font-weight: 500;
	}

	.file-size {
		color: var(--text-secondary);
		font-size: 14px;
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

	.progress-section {
		margin-top: 20px;
	}

	.progress-header {
		display: flex;
		justify-content: space-between;
		margin-bottom: 8px;
	}

	.progress-status {
		font-weight: 500;
		text-transform: capitalize;
	}

	.progress-bar {
		height: 8px;
		background: var(--bg-tertiary);
		border-radius: 4px;
		overflow: hidden;
	}

	.progress-fill {
		height: 100%;
		background: var(--accent);
		transition: width 0.3s ease;
	}

	.progress-message {
		margin-top: 8px;
		color: var(--text-secondary);
		font-size: 14px;
	}

	.convert-btn {
		margin-top: 20px;
		width: 100%;
		padding: 16px;
		font-size: 16px;
	}

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

	.empty-state {
		text-align: center;
		padding: 60px;
		color: var(--text-secondary);
	}
</style>
