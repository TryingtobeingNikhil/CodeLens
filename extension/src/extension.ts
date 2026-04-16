import * as vscode from 'vscode';
import { spawn, ChildProcess, execSync } from 'child_process';
import axios from 'axios';
import { SearchPanelProvider } from './searchPanel';

let backendProcess: ChildProcess | undefined;
let statusBarItem: vscode.StatusBarItem;
let pollInterval: NodeJS.Timeout;

const PORT = 8000;
const API_URL = `http://127.0.0.1:${PORT}`;

/** Find best available Python 3 (prefers 3.11, falls back to 3.12, then python3) */
function detectPython(): string {
    const candidates = [
        '/usr/local/bin/python3.11',
        '/usr/local/bin/python3.12',
        '/usr/local/bin/python3',
        '/usr/bin/python3',
        'python3',
    ];
    for (const p of candidates) {
        try {
            execSync(`${p} --version`, { stdio: 'ignore' });
            return p;
        } catch { /* try next */ }
    }
    return 'python3';
}

const PYTHON = detectPython();
const OLLAMA_BIN = '/Applications/Ollama.app/Contents/Resources/ollama';

export async function activate(context: vscode.ExtensionContext) {
    // 1. Boot Python Backend Context
    startBackendServer(context);

    // 2. Register Webview sidebar
    const provider = new SearchPanelProvider(context.extensionUri);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider("codelens.sidebar", provider)
    );

    // 3. Mount Status Bar precisely formatted
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBarItem.text = "$(sync~spin) CodeLens starting...";
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);

    // 4. Command Registrations
    context.subscriptions.push(vscode.commands.registerCommand('codelens.search', () => {
        vscode.commands.executeCommand('codelens.sidebar.focus');
    }));

    context.subscriptions.push(vscode.commands.registerCommand('codelens.reindex', () => {
        provider.triggerReindex();
    }));

    context.subscriptions.push(vscode.commands.registerCommand('codelens.showStatus', () => {
        vscode.commands.executeCommand('codelens.sidebar.focus');
    }));

    // 5. Pre-flight initialization & UI checks
    setTimeout(async () => {
        try {
            const health = await axios.get(`${API_URL}/健康`, { timeout: 1000 }).catch(() => axios.get(`${API_URL}/health`));
            
            if (health.data && health.data.ollama === false) {
                const actionBtn = "Install Ollama";
                const installRes = await vscode.window.showWarningMessage(
                    "CodeLens Engine Offline: Ollama was not found on your system. This is strictly required for local vector generation.",
                    actionBtn
                );
                if (installRes === actionBtn) {
                    vscode.env.openExternal(vscode.Uri.parse("https://ollama.ai")); // or ollama.com depending on routing
                }
            }
        } catch (e) {
            console.error("CodeLens Backend Health Check Missed");
        }
        
        startPolling();
    }, 4500); // giving uvicorn maximum startup leeway
}

function startBackendServer(context: vscode.ExtensionContext) {
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0].uri.fsPath;
    // The backend lives in the 'codelens' sub-folder of the workspace
    const cwd = workspaceRoot ? `${workspaceRoot}/codelens` : __dirname;

    backendProcess = spawn(
        PYTHON,
        ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', PORT.toString()],
        {
            cwd,
            detached: false,
            env: {
                ...process.env,
                PATH: `/Applications/Ollama.app/Contents/Resources:${process.env.PATH}`,
                OLLAMA_HOST: 'http://localhost:11434',
            }
        }
    );

    backendProcess.stdout?.on('data', (d) => console.log(`[CodeLens]: ${d}`));
    backendProcess.stderr?.on('data', (d) => console.error(`[CodeLens ERR]: ${d}`));
    backendProcess.on('exit', (code) => {
        console.warn(`[CodeLens] Backend exited with code ${code}`);
        statusBarItem.text = '$(database) CodeLens: Offline';
    });
}

function startPolling() {
    pollStatus();
    pollInterval = setInterval(pollStatus, 30000); // updates every 30s
}

async function pollStatus() {
    try {
        const res = await axios.get(`${API_URL}/status`);
        const chunks = res.data.indexed_chunks || 0;
        statusBarItem.text = `$(database) CodeLens: ${chunks} chunks`;
    } catch(e) {
        statusBarItem.text = `$(database) CodeLens: Offline`;
    }
}

export function deactivate() {
    if (pollInterval) clearInterval(pollInterval);
    if (backendProcess && !backendProcess.killed) {
        backendProcess.kill('SIGINT');
    }
}
