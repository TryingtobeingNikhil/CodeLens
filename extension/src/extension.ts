import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import { spawn, ChildProcess } from 'child_process';
import { CodeLensClient } from './client';
import { SearchPanelProvider } from './searchPanel';

let backendProcess: ChildProcess | undefined;
let statusBarItem: vscode.StatusBarItem;
let pollInterval: NodeJS.Timeout | undefined;
let output: vscode.OutputChannel;

const REQUIRED_MODULES = 'import fastapi, uvicorn, ollama, tree_sitter, numpy, watchdog, dotenv';

function config() {
    return vscode.workspace.getConfiguration('codelens');
}

function baseUrl(): string {
    return `http://127.0.0.1:${config().get<number>('port', 8000)}`;
}

export async function activate(context: vscode.ExtensionContext) {
    output = vscode.window.createOutputChannel('CodeLens');
    context.subscriptions.push(output);

    const client = new CodeLensClient(baseUrl);
    const provider = new SearchPanelProvider(context.extensionUri, client, output);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('codelens.sidebar', provider, {
            webviewOptions: { retainContextWhenHidden: true },
        }),
    );

    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBarItem.text = '$(sync~spin) CodeLens starting...';
    statusBarItem.command = 'codelens.search';
    statusBarItem.tooltip = 'Open CodeLens search';
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);

    context.subscriptions.push(
        vscode.commands.registerCommand('codelens.search', () => provider.focus()),
        vscode.commands.registerCommand('codelens.reindex', () => provider.reindex(false)),
        vscode.commands.registerCommand('codelens.rebuildIndex', () => provider.reindex(true)),
        vscode.commands.registerCommand('codelens.showStatus', () => output.show()),
        vscode.commands.registerCommand('codelens.searchSelection', () => {
            const editor = vscode.window.activeTextEditor;
            const text = editor?.document.getText(editor.selection).trim();
            if (text) { provider.search(text.slice(0, 2000)); }
        }),
        vscode.commands.registerCommand('codelens.explainSelection', () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) { return; }
            // No selection: explain the line the cursor is on, with a little context
            const range = editor.selection.isEmpty
                ? editor.document.lineAt(editor.selection.active.line).range
                : editor.selection;
            const code = editor.document.getText(range).trim();
            if (!code) {
                vscode.window.showInformationMessage('CodeLens: select some code to explain.');
                return;
            }
            const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
            const file = folder ? path.relative(folder.uri.fsPath, editor.document.uri.fsPath)
                : path.basename(editor.document.uri.fsPath);
            provider.explainSelection(code.slice(0, 12000), file.split(path.sep).join('/'), editor.document.languageId);
        }),
        vscode.commands.registerCommand('codelens.setupBackend', async () => {
            const dir = resolveBackendDir(context);
            if (!dir) { return; }
            if (await setUpManagedEnv(context, dir)) {
                stopBackend();
                await ensureBackend(context, client);
                provider.refreshStatus();
            }
        }),
        vscode.commands.registerCommand('codelens.restartBackend', async () => {
            stopBackend();
            await ensureBackend(context, client);
            provider.refreshStatus();
        }),
    );

    // Don't block activation on the backend: the panel reports it as offline until it's up
    ensureBackend(context, client).finally(() => {
        provider.refreshStatus();
        pollStatus(client, provider);
        pollInterval = setInterval(() => pollStatus(client, provider), 15000);
    });
}

/** Directory containing `backend/main.py`: the setting, else the extension's own folder. */
function resolveBackendDir(context: vscode.ExtensionContext): string | undefined {
    const candidates = [config().get<string>('backendPath', ''), context.extensionPath].filter(Boolean);
    return candidates.find(dir => fs.existsSync(path.join(dir, 'backend', 'main.py')));
}

function venvPython(venvDir: string): string {
    return process.platform === 'win32'
        ? path.join(venvDir, 'Scripts', 'python.exe')
        : path.join(venvDir, 'bin', 'python');
}

/** The private environment CodeLens creates for itself (survives extension updates). */
function managedVenvDir(context: vscode.ExtensionContext): string {
    return path.join(context.globalStorageUri.fsPath, 'venv');
}

function pythonCandidates(context: vscode.ExtensionContext, backendDir: string): string[] {
    const configured = config().get<string>('pythonPath', '');
    const local = venvPython(path.join(backendDir, '.venv'));
    const managed = venvPython(managedVenvDir(context));
    return [configured, fs.existsSync(local) ? local : '', fs.existsSync(managed) ? managed : '',
        'python3', 'python'].filter(Boolean);
}

/** Exit code of a short probe command, without blocking the extension host. */
function probe(cmd: string, args: string[], timeout: number, cwd?: string): Promise<number | null> {
    return new Promise(resolve => {
        const child = spawn(cmd, args, { cwd, timeout, stdio: 'ignore' });
        child.on('error', () => resolve(null));
        child.on('exit', code => resolve(code));
    });
}

async function findPython(context: vscode.ExtensionContext, backendDir: string): Promise<string | undefined> {
    for (const p of pythonCandidates(context, backendDir)) {
        if (await probe(p, ['-c', REQUIRED_MODULES], 20000, backendDir) === 0) { return p; }
    }
    return undefined;
}

/** A Python 3.10+ to create the private environment with. */
async function findBasePython(): Promise<string[] | undefined> {
    const candidates: string[][] = process.platform === 'win32'
        ? [['py', '-3'], ['python'], ['python3']]
        : [['python3.12'], ['python3.11'], ['python3.10'], ['python3'], ['python']];
    for (const [cmd, ...args] of candidates) {
        if (await probe(cmd, [...args, '-c',
            'import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)'], 10000) === 0) {
            return [cmd, ...args];
        }
    }
    return undefined;
}

/** Backend dependencies, read from the bundled pyproject.toml so they are listed once. */
function backendRequirements(backendDir: string): string[] {
    const toml = fs.readFileSync(path.join(backendDir, 'pyproject.toml'), 'utf-8');
    const block = /^dependencies\s*=\s*\[([\s\S]*?)^\]/m.exec(toml)?.[1] ?? '';
    return [...block.matchAll(/^\s*"([^"]+)"/gm)].map(m => m[1]);
}

function run(cmd: string, args: string[], token: vscode.CancellationToken): Promise<void> {
    return new Promise((resolve, reject) => {
        output.appendLine(`$ ${cmd} ${args.join(' ')}`);
        const child = spawn(cmd, args);
        token.onCancellationRequested(() => child.kill());
        child.stdout?.on('data', d => output.append(d.toString()));
        child.stderr?.on('data', d => output.append(d.toString()));
        child.on('error', reject);
        child.on('exit', code => code === 0 ? resolve()
            : reject(new Error(token.isCancellationRequested ? 'Cancelled' : `${cmd} exited with code ${code}`)));
    });
}

/**
 * Create a private virtualenv in the extension's storage and install the backend's
 * dependencies into it, so installing the extension is the only manual step.
 * Ollama is not installed: it is optional and left to the user.
 */
async function setUpManagedEnv(context: vscode.ExtensionContext, backendDir: string): Promise<boolean> {
    const base = await findBasePython();
    if (!base) {
        const choice = await vscode.window.showErrorMessage(
            'CodeLens needs Python 3.10 or newer to run its local search engine. Install it, then try again.',
            'Download Python');
        if (choice) { vscode.env.openExternal(vscode.Uri.parse('https://www.python.org/downloads/')); }
        return false;
    }
    const venvDir = managedVenvDir(context);
    const python = venvPython(venvDir);
    output.show(true);
    try {
        await vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: 'CodeLens: setting up the search engine',
            cancellable: true,
        }, async (progress, token) => {
            fs.mkdirSync(context.globalStorageUri.fsPath, { recursive: true });
            progress.report({ message: 'creating a private Python environment' });
            await run(base[0], [...base.slice(1), '-m', 'venv', venvDir], token);
            progress.report({ message: 'installing packages (about a minute)' });
            await run(python, ['-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip'], token);
            await run(python, ['-m', 'pip', 'install', '--disable-pip-version-check',
                ...backendRequirements(backendDir)], token);
        });
        output.appendLine(`CodeLens environment ready: ${python}`);
        return true;
    } catch (e: any) {
        output.appendLine(`Setup failed: ${e?.message ?? e}`);
        vscode.window.showErrorMessage(`CodeLens setup failed: ${e?.message ?? e}`, 'Show Logs')
            .then(c => { if (c) { output.show(); } });
        return false;
    }
}

async function ensureBackend(context: vscode.ExtensionContext, client: CodeLensClient): Promise<void> {
    if (await client.health()) {
        output.appendLine(`Using the CodeLens backend already running at ${baseUrl()}`);
        return;
    }
    if (!config().get<boolean>('autoStartBackend', true)) {
        output.appendLine('Backend not running and codelens.autoStartBackend is off.');
        return;
    }

    const backendDir = resolveBackendDir(context);
    if (!backendDir) {
        const choice = await vscode.window.showErrorMessage(
            'CodeLens: backend not found. Set "codelens.backendPath" to the folder that contains backend/main.py.',
            'Open Settings');
        if (choice) { vscode.commands.executeCommand('workbench.action.openSettings', 'codelens.backendPath'); }
        return;
    }

    let python = await findPython(context, backendDir);
    if (!python) {
        output.appendLine('No Python environment with the backend dependencies was found.');
        const choice = await vscode.window.showInformationMessage(
            'CodeLens needs to install its local search engine (Python packages, about 150 MB) ' +
            'into a private environment. Nothing is installed system-wide.',
            'Set Up', 'Use My Own Python');
        if (choice === 'Use My Own Python') {
            vscode.commands.executeCommand('workbench.action.openSettings', 'codelens.pythonPath');
            return;
        }
        if (choice !== 'Set Up' || !(await setUpManagedEnv(context, backendDir))) { return; }
        python = await findPython(context, backendDir);
        if (!python) {
            vscode.window.showErrorMessage('CodeLens: the environment was created but the backend still cannot ' +
                'load its packages.', 'Show Logs').then(c => { if (c) { output.show(); } });
            return;
        }
    }

    const port = String(config().get<number>('port', 8000));
    output.appendLine(`Starting backend: ${python} -m uvicorn backend.main:app --port ${port} (in ${backendDir})`);
    const startedAt = Date.now();
    backendProcess = spawn(python, ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', port], {
        cwd: backendDir,
        env: {
            ...process.env,
            ...backendEnv(),
            // The Ollama desktop app keeps its CLI here on macOS
            PATH: `/Applications/Ollama.app/Contents/Resources${path.delimiter}${process.env.PATH ?? ''}`,
        },
    });
    backendProcess.stdout?.on('data', d => output.append(d.toString()));
    backendProcess.stderr?.on('data', d => output.append(d.toString()));
    backendProcess.on('exit', code => {
        output.appendLine(`Backend exited with code ${code}`);
        backendProcess = undefined;
        statusBarItem.text = '$(database) CodeLens: Offline';
        if (Date.now() - startedAt < 15000 && code !== 0 && code !== null) {
            vscode.window.showErrorMessage('CodeLens: the backend failed to start.', 'Show Logs')
                .then(choice => { if (choice) { output.show(); } });
        }
    });

    for (let i = 0; i < 40 && backendProcess; i++) {
        if (await client.health(1000)) { return; }
        await new Promise(r => setTimeout(r, 500));
    }
}

/** Settings forwarded to a backend the extension starts itself. */
function backendEnv(): Record<string, string> {
    const env: Record<string, string> = {};
    const host = config().get<string>('ollamaHost', '');
    const model = config().get<string>('explainModel', '');
    if (host) { env.OLLAMA_HOST = host; }
    if (model) { env.EXPLAIN_MODEL = model; }
    return env;
}

async function pollStatus(client: CodeLensClient, provider: SearchPanelProvider) {
    const health = await client.health();
    if (!health) {
        statusBarItem.text = '$(database) CodeLens: Offline';
        return;
    }
    try {
        const status = await client.status(provider.repoRoot());
        statusBarItem.text = status.indexing
            ? '$(sync~spin) CodeLens: indexing'
            : `$(database) CodeLens: ${status.indexed_chunks} chunks`;
        statusBarItem.tooltip = health.ollama
            ? `${status.files} files · ${status.embedded_chunks}/${status.indexed_chunks} embedded · ${status.vector_backend}`
            : 'Ollama is offline: keyword search only';
    } catch {
        statusBarItem.text = '$(database) CodeLens: Offline';
    }
}

function stopBackend() {
    if (backendProcess && !backendProcess.killed) {
        backendProcess.kill('SIGINT');
    }
    backendProcess = undefined;
}

export function deactivate() {
    if (pollInterval) { clearInterval(pollInterval); }
    stopBackend();
}
