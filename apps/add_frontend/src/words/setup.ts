// Set up a device words (08-advanced W3, prepare.md §3l-§3m, code-keyed part).
// State words live in STATE_WORDS.setup / STATE_WORDS.boxType; this file adds
// the server messages (matched by text; backend strings stay as they are), the
// result error codes, and the "switched off" words (D-17/D-18: the menu item
// stays with a grey "Switched off" tag while provisioning is off).

import { plainStatus } from './states'
import type { PlainStatus } from './types'

export const setupState = (state: string | null | undefined): PlainStatus => plainStatus('setup', state)
export const boxType = (classification: string | null | undefined): PlainStatus => plainStatus('boxType', classification)

export const SETUP_SWITCHED_OFF = {
  tag: 'Switched off',
  page: 'Setting up new link boxes is switched off right now.',
} as const

/** Result card error codes. */
export const SETUP_ERRORS: Record<string, string> = {
  PACKAGE_PREPARATION_FAILED: "The dashboard couldn't prepare the setup. Nothing was changed on the box.",
  ONBOARDING_IDENTITY_MISMATCH: 'The box came online with different place details than this setup. Ask IT to check it.',
  FIRMWARE_FAMILY_MISMATCH: "The software doesn't match the machine brand you chose. Start again and pick the right brand.",
}

/** Server messages that reach the setup screens (matched by exact text or prefix). */
export const SETUP_MESSAGES: Array<{ match: string | RegExp; sentence: string }> = [
  { match: 'Physical provisioning is disabled.', sentence: 'Setting up link boxes from the dashboard is switched off.' },
  { match: 'Provisioning session not found.', sentence: 'This setup no longer exists.' },
  { match: 'Provisioning companion not found.', sentence: 'This setup app is no longer connected.' },
  { match: 'Pairing code is invalid or expired.', sentence: 'That code is wrong or too old. Get a new code from the setup app.' },
  { match: 'Select a paired companion.', sentence: 'Choose a connected setup app.' },
  { match: 'Provisioning companion update required.', sentence: 'Update the setup app first (download it again above).' },
  { match: 'Idempotency key belongs to another session.', sentence: 'Something went wrong starting the setup. Refresh and try again.' },
  { match: /^You already have active session /, sentence: 'You already have a setup running. Finish or cancel it first.' },
  { match: /^Companion already has active session /, sentence: 'This setup app is busy with another setup.' },
  { match: 'No approved factory bundle is available.', sentence: 'No software for new boxes is available yet. Ask IT.' },
  { match: 'This session belongs to another operator.', sentence: 'Someone else started this setup.' },
  { match: 'Device inspection must finish first.', sentence: 'Wait until the box check finishes.' },
  { match: 'Detected ESP identity is missing.', sentence: "The box wasn't read properly. Unplug it, plug it back in and start again." },
  { match: 'Selected factory bundle is unavailable.', sentence: 'The software for new boxes was withdrawn. Start again.' },
  { match: 'Selected factory bundle was revoked.', sentence: 'The software for new boxes was withdrawn. Start again.' },
  { match: 'FIRMWARE_FAMILY_MISMATCH', sentence: SETUP_ERRORS.FIRMWARE_FAMILY_MISMATCH },
  { match: /^Device ID .+ is already active in zone /, sentence: 'This device number is already used at that place. Choose another number.' },
  { match: 'Session is not awaiting authorization.', sentence: 'This step is already done. Refresh the page.' },
  { match: 'Confirm the physical label and type the exact detected MAC.', sentence: 'Tick the label box and type the box ID exactly as printed.' },
  { match: 'Protected configuration expired; review the configuration again.', sentence: 'The details timed out. Please enter the passwords again.' },
  { match: 'Protected package preparation failed; no device write was started.', sentence: "The dashboard couldn't prepare the setup. Nothing was changed on the box. Try again." },
  { match: 'Package is ready, but the companion disconnected. Reconnect to resume.', sentence: 'The setup app closed. Open it again on this computer; setup will continue.' },
  { match: 'Cancellation is unavailable after an irreversible operation starts.', sentence: "It's too late to cancel: the box is being set up. Please wait." },
  { match: 'Session is not waiting for terminal confirmation.', sentence: 'The machine is already confirmed.' },
  { match: 'Observed terminal serial changed; refresh the authenticated terminal evidence.', sentence: 'The machine details changed. Refresh and check again.' },
  { match: 'That terminal serial is already pinned.', sentence: 'This machine is already linked to another link box.' },
  { match: 'Password confirmation failed.', sentence: 'Wrong password. Try again.' },
]

/** Plain sentence for a setup server message or code, or null when it has no words. */
export function setupMessage(message: string | null | undefined): string | null {
  const text = (message ?? '').trim()
  if (!text) return null
  if (SETUP_ERRORS[text]) return SETUP_ERRORS[text]
  const found = SETUP_MESSAGES.find(({ match }) => (typeof match === 'string' ? text === match : match.test(text)))
  return found ? found.sentence : null
}
