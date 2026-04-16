import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import axios from 'axios';
import * as http from 'http';

export class SearchPanelProvider implements vscode.WebviewViewProvider {
    private _view?: vscode.WebviewView;

    constructor(private readonly _extensionUri: vscode.Uri) {}

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ) {
        this._view = webviewView;
        webviewView.webview.options = { 
            enableScripts: true,
            localResourceRoots: [this._extensionUri]
        };

        webviewView.webview.html = this.getHtmlForWebview();

        // 1. Process Messages dispatched from panel.html WebView runtime
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'query':
                    this.handleQuery(data.text, data.explain);
                    break;
                case 'jumpTo':
                    this.handleJumpTo(data.file, data.line);
                    break;
                case 'reindex':
                    this.handleReindex();
                    break;
            }
        });
    }

    public triggerReindex() {
        this.handleReindex();
    }

    private async handleQuery(text: string, explain: boolean) {
        try {
            // Native Axios bindings directly parsing localhost backend
            const res = await axios.post(`http://127.0.0.1:8000/query`, {
                query: text,
                top_k: 8,
                explain: explain
            });
            this._view?.webview.postMessage({
                type: 'results',
                data: res.data.results,
                explain_text: res.data.explain_text,
                query_ms: res.data.query_ms,
                total_indexed: res.data.total_indexed
            });
        } catch (error: any) {
             this._view?.webview.postMessage({ type: 'error', message: error.message });
        }
    }

    private async handleReindex() {
        const workspaceDir = vscode.workspace.workspaceFolders?.[0].uri.fsPath;
        if (!workspaceDir) {
            this._view?.webview.postMessage({ type: 'error', message: 'No valid workspace currently open.' });
            return;
        }

        const payload = JSON.stringify({ repo_path: workspaceDir, force_reindex: true });
        
        // Native http mapping capturing raw text/event-stream signals from Uvicorn without Axios block mapping abstractions
        const req = http.request('http://127.0.0.1:8000/index', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(payload)
            }
        }, (res) => {
            res.on('data', (bufferChunk) => {
                const events = bufferChunk.toString().split('\n\n');
                
                for (const ev of events) {
                    if (ev.startsWith('data: ')) {
                        try {
                            const parsed = JSON.parse(ev.substring(6));
                            if(parsed.type === "progress") {
                                // Maps exactly onto the Webview message protocol specified
                                this._view?.webview.postMessage({
                                    type: 'indexProgress',
                                    file: parsed.file,
                                    pct: (parsed.chunks / Math.max(1, parsed.total_files)) * 100,
                                    message: `${parsed.chunks} chunks found`
                                });
                            } else if (parsed.type === "complete") {
                                this._view?.webview.postMessage({
                                    type: 'status',
                                    chunks: parsed.total_chunks,
                                    watching: true
                                });
                            } else if (parsed.type === "error") {
                                this._view?.webview.postMessage({ type: 'error', message: parsed.message });
                            }
                        } catch (e) {
                            // Ignored partial streaming fragments
                        }
                    }
                }
            });
        });
        
        req.on('error', (e) => {
             this._view?.webview.postMessage({ type: 'error', message: 'Failed maintaining local index SSE connection.' });
        });
        
        req.write(payload);
        req.end();
    }

    private async handleJumpTo(file: string, line: number) {
        const workspace = vscode.workspace.workspaceFolders?.[0].uri.fsPath;
        if (!workspace) return;
        
        const absolutePath = path.join(workspace, file);
        try {
            const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(absolutePath));
            const editor = await vscode.window.showTextDocument(doc);
            
            // Adjust to VS Code internal 0-indexed engine structure safely mapping bounds
            const vsLine = Math.max(0, line - 1);
            const range = new vscode.Range(vsLine, 0, vsLine, 0);
            
            editor.selection = new vscode.Selection(range.start, range.end);
            editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
        } catch (e: any) {
            vscode.window.showErrorMessage(`Cannot open file: ${e.message}`);
        }
    }

    private getHtmlForWebview() {
        const htmlPath = vscode.Uri.joinPath(this._extensionUri, 'media', 'panel.html');
        return fs.readFileSync(htmlPath.fsPath, 'utf-8');
    }
}
