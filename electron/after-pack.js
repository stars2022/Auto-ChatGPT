const fs = require('fs');
const path = require('path');

/** Copy a native PyInstaller backend into the packaged app when CI produced one. */
exports.default = async function afterPack(context) {
  const platform = context.electronPlatformName || process.platform;
  const executable = platform === 'win32' ? 'autocodex-backend.exe' : 'autocodex-backend';
  const projectDir = context.packager?.projectDir || process.cwd();
  const source = path.join(projectDir, 'build', 'backend-dist', platform, executable);
  if (!fs.existsSync(source)) {
    console.log(`[afterPack] no native backend at ${source}; packaged app will use Python fallback`);
    return;
  }
  const resourcesRoot = platform === 'darwin'
    ? path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`, 'Contents', 'Resources')
    : path.join(context.appOutDir, 'resources');
  const destinationDir = path.join(resourcesRoot, 'backend');
  fs.mkdirSync(destinationDir, { recursive: true });
  const destination = path.join(destinationDir, executable);
  fs.copyFileSync(source, destination);
  if (platform !== 'win32') fs.chmodSync(destination, 0o755);
  console.log(`[afterPack] bundled native backend ${destination}`);
};
