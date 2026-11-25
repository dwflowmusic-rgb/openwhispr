# Auto-Enter + Auto-Patcher para OpenWhispr (Windows)

Este diretório complementa a implementação nativa da chave **`autoEnterEnabled`** no código-fonte deste fork (`settingsStore.ts`, `useSettings.ts`, `SettingsPage.tsx`, `useAudioRecording.js`, `ipcHandlers.js`, `clipboard.js`, `resources/windows-fast-paste.c` e traduções PT/EN) com um **auto-patcher automático** para instalações oficiais pré-compiladas do OpenWhispr no Windows.

## Por que o Auto-Patcher existe?

Quando você usa o instalador oficial assinado do OpenWhispr (com atualizações automáticas ativas), cada atualização substitui `C:\Program Files\OpenWhispr\resources\app.asar`. Como os *Electron Fuses* atuais do executável oficial têm `EnableEmbeddedAsarIntegrityValidation = 0`, é possível reaplicar o patch automaticamente logo após cada atualização sem bloquear updates nem quebrar o aplicativo.

## Arquivos

- `patch_openwhispr.py` — Aplica o patch no `app.asar` localizando os bundles Vite dinamicamente por prefixo (`settingsStore-*.js`, `index-*.js`, `SettingsModal-*.js`, `i18n-*.js`), injeta o toggle **`Pressionar Enter após colar`** (chave `autoEnterEnabled` no `localStorage`), desativa o espaço final do *Smart Spacing* quando `pressEnter` está ativo, adiciona o envio de `Enter` na fila de colagem e instala o gancho `--wait-update` em `src/updater.js`.
- `src/WindowsPressEnter.cs` — Helper nativo Win32 (`SendInput` com `VK_RETURN`), compilável com o `csc.exe` do .NET Framework 4.x sem dependências externas, que aguarda a liberação de modificadores (`Ctrl`, `Shift`, `Alt`, `Win`) antes de enviar `Enter`.
- `install-guard.ps1` — Registra a tarefa agendada `OpenWhispr-AutoEnter-Guard` no Windows (execução via `pythonw.exe` no logon e a cada 10 minutos com verificação rápida de carimbo `<10 ms`).

## Uso rápido no Windows

1. Copie os arquivos para `C:\Users\giand\.openwhispr\auto-enter-patch\`.
2. Compile o helper Win32:
   ```powershell
   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /optimize+ /target:exe /out:"$env:USERPROFILE\.openwhispr\auto-enter-patch\bin\windows-press-enter.exe" "$env:USERPROFILE\.openwhispr\auto-enter-patch\src\WindowsPressEnter.cs"
   ```
3. Aplique e verifique o patch:
   ```powershell
   python "$env:USERPROFILE\.openwhispr\auto-enter-patch\patch_openwhispr.py" --apply
   python "$env:USERPROFILE\.openwhispr\auto-enter-patch\patch_openwhispr.py" --verify
   ```
4. Instale a proteção contínua no Agendador de Tarefas (PowerShell como Administrador):
   ```powershell
   powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.openwhispr\auto-enter-patch\install-guard.ps1"
   ```
