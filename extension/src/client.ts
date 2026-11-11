import type {
    ExplainRequest, ExplainResponse, HealthResponse, IndexEvent, QueryRequest, QueryResponse,
    StatusResponse, SuggestionsResponse,
} from '../../shared/types';

export class BackendError extends Error {
    constructor(message: string, readonly status?: number) {
        super(message);
    }
}

/** Thin HTTP client for the local CodeLens backend (Node's built-in fetch). */
export class CodeLensClient {
    constructor(private readonly baseUrl: () => string) {}

    private async request<T>(path: string, init: RequestInit = {}, timeoutMs = 30000): Promise<T> {
        const signals = [AbortSignal.timeout(timeoutMs)];
        if (init.signal) { signals.push(init.signal); }
        const res = await fetch(this.baseUrl() + path, {
            ...init,
            signal: anySignal(signals),
            headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...(init.headers ?? {}) },
        });
        if (!res.ok) {
            let detail = res.statusText;
            try { detail = ((await res.json()) as { detail?: string }).detail ?? detail; } catch { /* not json */ }
            throw new BackendError(detail, res.status);
        }
        return (await res.json()) as T;
    }

    async health(timeoutMs = 1500): Promise<HealthResponse | undefined> {
        try {
            const res = await this.request<HealthResponse>('/health', {}, timeoutMs);
            // Another program on the same port may also answer /health
            return typeof res?.db === 'boolean' && 'vector_backend' in res ? res : undefined;
        } catch {
            return undefined;
        }
    }

    /** True when something (not necessarily CodeLens) answers HTTP on the port. */
    async portInUse(timeoutMs = 1500): Promise<boolean> {
        try {
            await fetch(this.baseUrl() + '/', { signal: AbortSignal.timeout(timeoutMs) });
            return true;
        } catch {
            return false;
        }
    }

    status(repo?: string): Promise<StatusResponse> {
        const q = repo ? `?repo_path=${encodeURIComponent(repo)}` : '';
        return this.request<StatusResponse>(`/status${q}`, {}, 5000);
    }

    query(body: QueryRequest, signal?: AbortSignal): Promise<QueryResponse> {
        // Explain mode waits on a local LLM, which can be slow
        return this.request<QueryResponse>('/query', {
            method: 'POST', body: JSON.stringify(body), signal,
        }, body.explain ? 180000 : 30000);
    }

    /** Explain a snippet (or the top results of a query) with the local LLM, which can be slow. */
    explain(body: ExplainRequest, signal?: AbortSignal): Promise<ExplainResponse> {
        return this.request<ExplainResponse>('/explain', {
            method: 'POST', body: JSON.stringify(body), signal,
        }, 180000);
    }

    /** Example searches built from what is indexed for this repo. */
    suggestions(repo?: string): Promise<SuggestionsResponse> {
        const q = repo ? `?repo_path=${encodeURIComponent(repo)}` : '';
        return this.request<SuggestionsResponse>(`/suggestions${q}`, {}, 5000);
    }

    /** Stream indexing events (server-sent events). */
    async *index(repo: string, force: boolean, signal?: AbortSignal): AsyncGenerator<IndexEvent> {
        const res = await fetch(this.baseUrl() + '/index', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_path: repo, force_reindex: force }),
            signal,
        });
        if (!res.ok || !res.body) {
            let detail = res.statusText;
            try { detail = ((await res.json()) as { detail?: string }).detail ?? detail; } catch { /* not json */ }
            throw new BackendError(detail, res.status);
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { value, done } = await reader.read();
            if (done) { break; }
            buffer += decoder.decode(value, { stream: true });
            // An event may arrive split across reads: only consume complete ones
            let sep: number;
            while ((sep = buffer.indexOf('\n\n')) !== -1) {
                const raw = buffer.slice(0, sep);
                buffer = buffer.slice(sep + 2);
                const data = raw.split('\n').filter(l => l.startsWith('data: ')).map(l => l.slice(6)).join('\n');
                if (data) { yield JSON.parse(data) as IndexEvent; }
            }
        }
    }
}

function anySignal(signals: AbortSignal[]): AbortSignal {
    const controller = new AbortController();
    for (const s of signals) {
        if (s.aborted) { controller.abort(s.reason); break; }
        s.addEventListener('abort', () => controller.abort(s.reason), { once: true });
    }
    return controller.signal;
}
