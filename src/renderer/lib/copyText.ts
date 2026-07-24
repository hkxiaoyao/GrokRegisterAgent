/**
 * Copy text to clipboard with Electron-friendly fallback.
 * navigator.clipboard can fail when focus/permission is missing; execCommand still works.
 */
export async function copyText(text: string): Promise<void> {
  const value = String(text ?? '');
  if (!value) {
    throw new Error('empty');
  }

  // Preferred path
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return;
    }
  } catch {
    // fall through
  }

  // Fallback: temporary textarea + execCommand('copy')
  if (typeof document === 'undefined') {
    throw new Error('no document');
  }
  const ta = document.createElement('textarea');
  ta.value = value;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  ta.style.top = '0';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  ta.setSelectionRange(0, value.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } finally {
    document.body.removeChild(ta);
  }
  if (!ok) {
    throw new Error('execCommand copy failed');
  }
}
