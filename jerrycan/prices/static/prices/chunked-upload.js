/**
 * Chunked Upload Manager
 * Splits large files into chunks and uploads them sequentially
 */

class ChunkedUploadManager {
    constructor(options = {}) {
        this.chunkSize = options.chunkSize || 5 * 1024 * 1024; // 5MB default
        this.onStart = options.onStart || (() => {});
        this.onProgress = options.onProgress || (() => {});
        this.onComplete = options.onComplete || (() => {});
        this.onError = options.onError || (() => {});
    }

    /**
     * Upload a file in chunks
     * @param {File} file - The file to upload
     * @returns {Promise} Resolves with task ID when upload is complete
     */
    async uploadFile(file) {
        const uploadId = this.generateUUID();
        const totalChunks = Math.ceil(file.size / this.chunkSize);

        console.log(`Starting upload: ${file.name} (${this.formatBytes(file.size)})`);
        console.log(`Total chunks: ${totalChunks}, chunk size: ${this.formatBytes(this.chunkSize)}`);
        this.onStart({ uploadId, filename: file.name, totalChunks });

        try {
            // Upload each chunk
            for (let i = 0; i < totalChunks; i++) {
                const start = i * this.chunkSize;
                const end = Math.min(start + this.chunkSize, file.size);
                const chunk = file.slice(start, end);

                await this.uploadChunk(uploadId, i, totalChunks, chunk, file.name);

                const progress = ((i + 1) / totalChunks) * 100;
                this.onProgress({
                    chunk: i + 1,
                    totalChunks: totalChunks,
                    progress: progress,
                    bytesUploaded: end,
                    totalBytes: file.size,
                });
            }

            // Finalize upload
            const taskId = await this.finalizeUpload(uploadId, file.name);
            this.onComplete({ taskId, uploadId, filename: file.name });
            return taskId;

        } catch (error) {
            console.error('Upload failed:', error);
            this.onError({ error: error.message, uploadId });
            throw error;
        }
    }

    /**
     * Upload a single chunk
     * @private
     */
    async uploadChunk(uploadId, chunkNumber, totalChunks, blob, filename) {
        const formData = new FormData();
        formData.append('uploadId', uploadId);
        formData.append('chunkNumber', chunkNumber);
        formData.append('totalChunks', totalChunks);
        formData.append('chunk', blob, filename);

        // Add CSRF token for POST requests
        const csrfToken = this.getCSRFToken();
        if (csrfToken) {
            formData.append('csrfmiddlewaretoken', csrfToken);
        }

        const response = await fetch('/api/upload/chunk/', {
            method: 'POST',
            body: formData,
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(`Chunk ${chunkNumber} failed: ${error.error}`);
        }

        const result = await response.json();
        console.log(`Chunk ${chunkNumber}/${totalChunks} uploaded`);
        return result;
    }

    /**
     * Finalize the upload and trigger import
     * @private
     */
    async finalizeUpload(uploadId, filename) {
        const csrfToken = this.getCSRFToken();
        const headers = {
            'Content-Type': 'application/json',
        };
        if (csrfToken) {
            headers['X-CSRFToken'] = csrfToken;
        }

        const response = await fetch('/api/upload/complete/', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({
                uploadId: uploadId,
                filename: filename,
            }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(`Finalize failed: ${error.error}`);
        }

        const result = await response.json();
        console.log('Upload finalized, import started:', result);
        return result.taskId;
    }

    /**
     * Get CSRF token from DOM
     * @private
     */
    getCSRFToken() {
        // Try to get from csrftoken cookie
        const name = 'csrftoken';
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        // Try to get from hidden input (Django template)
        if (!cookieValue) {
            const token = document.querySelector('[name=csrfmiddlewaretoken]');
            if (token) {
                cookieValue = token.value;
            }
        }
        return cookieValue;
    }

    /**
     * Generate a UUID for upload tracking
     * @private
     */
    generateUUID() {
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    /**
     * Format bytes to human readable size
     * @private
     */
    formatBytes(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
    }
}

// Export for use in templates
window.ChunkedUploadManager = ChunkedUploadManager;
