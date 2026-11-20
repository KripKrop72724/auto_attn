// One modal lock and one layer stack for every overlay in the kit (Dialog,
// Sheet). The app root becomes inert and hidden from screen readers while any
// modal layer is open, the scrolling workspace stops scrolling, and both come
// back exactly as they were when the last layer closes.

let modalLockDepth = 0
let lockedWorkspace: HTMLElement | null = null
let lockedWorkspaceOverflow = ''
let lockedWorkspaceScrollTop = 0
let rootAriaHidden: string | null = null
let rootWasInert = false

export function lockApplicationForModal() {
  const root = document.getElementById('root')
  if (modalLockDepth === 0) {
    lockedWorkspace = document.querySelector<HTMLElement>('.app-workspace')
    lockedWorkspaceOverflow = lockedWorkspace?.style.overflow || ''
    lockedWorkspaceScrollTop = lockedWorkspace?.scrollTop || 0
    if (lockedWorkspace) lockedWorkspace.style.overflow = 'hidden'
    if (root) {
      rootAriaHidden = root.getAttribute('aria-hidden')
      rootWasInert = root.hasAttribute('inert')
      root.setAttribute('aria-hidden', 'true')
      root.setAttribute('inert', '')
    }
  }
  modalLockDepth += 1
  let released = false
  return () => {
    if (released) return
    released = true
    modalLockDepth = Math.max(0, modalLockDepth - 1)
    if (modalLockDepth > 0) return
    if (lockedWorkspace) {
      lockedWorkspace.style.overflow = lockedWorkspaceOverflow
      lockedWorkspace.scrollTop = lockedWorkspaceScrollTop
    }
    if (root) {
      if (rootAriaHidden === null) root.removeAttribute('aria-hidden')
      else root.setAttribute('aria-hidden', rootAriaHidden)
      if (!rootWasInert) root.removeAttribute('inert')
    }
    lockedWorkspace = null
  }
}

// Open layers, innermost last: a layer opened from another one (for example an
// action dialog inside the device panel) owns Escape and the focus trap.
const openLayers: symbol[] = []

export function pushLayer(token: symbol) {
  openLayers.push(token)
  return () => {
    const position = openLayers.indexOf(token)
    if (position >= 0) openLayers.splice(position, 1)
  }
}

export const isTopLayer = (token: symbol) => openLayers[openLayers.length - 1] === token

export const overlayRoot = () => document.getElementById('overlay-root') || document.body

export function prefersReducedMotion() {
  try {
    return typeof window.matchMedia === 'function' && Boolean(window.matchMedia('(prefers-reduced-motion: reduce)')?.matches)
  } catch {
    return false
  }
}

// Exit motion without keeping the real layer mounted: a hidden, inert copy of
// the closing layer fades out and is removed. Only where the Web Animations API
// exists (the production CSP allows element.animate, not injected styles).
export function playExitGhost(node: HTMLElement | null, panelSelector: string, panelKeyframes: Keyframe[]) {
  if (!node || !node.isConnected || typeof node.animate !== 'function' || prefersReducedMotion()) return
  const ghost = node.cloneNode(true) as HTMLElement
  ghost.querySelectorAll('[id]').forEach((element) => element.removeAttribute('id'))
  ghost.removeAttribute('id')
  ghost.querySelectorAll('[role]').forEach((element) => element.removeAttribute('role'))
  ghost.setAttribute('aria-hidden', 'true')
  ghost.setAttribute('inert', '')
  ghost.classList.add('is-exiting')
  overlayRoot().appendChild(ghost)
  const panel = ghost.querySelector<HTMLElement>(panelSelector)
  const timing: KeyframeAnimationOptions = { duration: 150, easing: 'cubic-bezier(.4, 0, 1, 1)', fill: 'forwards' }
  try {
    panel?.animate(panelKeyframes, timing)
    const fade = ghost.animate([{ opacity: 1 }, { opacity: 0 }], timing)
    fade.onfinish = () => ghost.remove()
    fade.oncancel = () => ghost.remove()
  } catch {
    ghost.remove()
  }
  window.setTimeout(() => ghost.remove(), 400)
}
