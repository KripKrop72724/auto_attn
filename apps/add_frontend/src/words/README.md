# Plain words (`src/words`)

Every code the dashboard can show (device states, problem codes, log codes, request and update states, refusal
codes …) is turned into plain words here, following the glossary and writing rules in `glossary.ts` (02-words,
design brief). Pages never print a code, a hash, an IP, a MAC or a file name outside a `data-technical` element
(`TechnicalDetails` in the kit).

Import the module you need directly (there is no index): `import { plainStatus } from '../words/states'`.
The words modules import only `../types`, `../status` and `../lib/time`; they never import features, `ui` or pages.
To keep the first chunk small, import `activity.ts`, `software.ts`, `recover.ts` and `attendanceReasons.ts` only
from lazy feature chunks. App code must never import `codes.generated.json` (tests only).

## Which module

| Module | Use it for |
|---|---|
| `states.ts` | `plainStatus(domain, code)` → `{ label, tone, explain, rank?, code, domain, unknown }`. One call for every status-like code (domains are the `Domain` union in `types.ts`: `device`, `tier`, `machineLink`, `delivery`, `identity`, `request`, `update`, `rollout`, `recoverJob`, …). An unknown code reads "Other status" (neutral) and never shows the code. Also `NOT_REPORTED`, `hasStatusWords`. |
| `types.ts` | `Tone` (`good` / `neutral` / `warning` / `problem`) and `toneToPattern` (good = confirmed, neutral = notice, warning = waiting, problem = blocked). The kit's `StatusChip` uses its own tones: bridge with `toneForPattern(toneToPattern(word.tone))` or `toneForPattern(statusPattern(code))`. |
| `deviceStatus.ts` | `deviceStatus(row)` → `{ group, chip, tone, heading, sentence, since, note, rank, code }` for Home and Devices (HOME-7, D-1). Order: Spare, Blocked: duplicate device, Offline, Setting up, Needs attention, Working. A machine that is not responding counts as Offline with the `/status` reasons word for word; when the row carries the backend `public_status`, its answer wins. `DEVICE_STATUS_FILTERS`, `offlineReasonCode`. |
| `machine.ts` | `plainMachineLink(link, now)` (label, tone, sentence, "for 12 min"), `plainConnectionRow`, `plainActivity(device, now)` ("Offline since 10:42 am · 19 h"), `offlineSince`. The backend message goes to Technical details only. |
| `health.ts` | Plain versions: `plainTier`, `plainReasonLine`, `healthDetailWords`, `whyTitle`, `deviceErrorLine`. Also the old App.tsx helpers with today's sentences (`terminalLinkLabel`, `healthReasonLine`, `resolutionSummary`, `formatAlertDiagnostics`, …) so unchanged pages keep working; move callers to the plain versions. |
| `problems.ts` | `problemWords({ code, plain_title?, plain_detail?, next_step?, … })`: prefers the backend `plain_*` fields (`be/problem_words.py`, which also builds titles with numbers), else the static `PROBLEM_INFO`, else the group fallback. `PROBLEM_LINKS`, `GROUP_LABELS`, `resolutionWords`, `refusalWords`, `seenByLine`. |
| `activity.ts` | `describeLog(row)` and `logWords(code)` for the Activity tab (the server's `/activity` items from `be/activity_words.py` are the full source; this is the fallback). |
| `attendance.ts` | `deliveryGroup(status)` (sent / waiting / not matched / set aside / problem, the same groups as `be/attendance_groups.py`), `deliveryStatus`, `checkinType`. |
| `attendanceReasons.ts` | Force release, Send saved check-ins and recovery reason sentences (`plainReason(domain, code)`). |
| `people.ts` | `EMPLOYEE_ID` ("Employee ID" = the machine's number, D-24) and `HR_EMPLOYEE_ID`, `writesOff(code)`, `personProblems`, `peopleError`. |
| `requests.ts` | Requests sent to a device: `requestTitle`, `requestStatus`, `requestStep`, `requestFailure`, the machine password (COMM Key) words. |
| `software.ts`, `recover.ts`, `setup.ts` | Software updates, Recover attendance and Set up a device words. |
| `errors.ts` | `errorWords(code)` for 409 refusal codes (sentence + "Try again" / "Check again"). |
| `glossary.ts` | `GLOSSARY`, `BANNED_WORDS` (one entry per line: the backend tests read it with a regex), `PLAIN_RULES`, `findPlainWordProblems`, `isPlainText`, `plural` / `pluralWord`, `formatCount`, `BUSY`. |
| `time.ts` | Small Pakistan-time helpers used inside sentences (`clockText`, `dayTimeText`, `durationText`, `agoText`). Pages use `lib/time.ts`. |

## Tests for a page

`import { expectPlainText } from '../words/testing'` then `expectPlainText(container)` fails with a readable list
when visible text or `aria-label` / `title` / `placeholder` / `alt` breaks a rule. It skips `[data-technical]`,
`[data-brand]`, `[data-device-code]` and `[data-user-text]` (names people typed).

## Adding a code (the words guard, D-22)

1. Add the code in the backend or firmware as usual.
2. Run `python scripts/export_word_codes.py` (inside the `add-test-env:local` image). It rewrites
   `src/words/codes.generated.json` and `apps/add_backend/zk_add/word_codes.json`, which must stay identical.
3. Add its words in the right module here. `words.test.ts` fails for a dashboard or backend code with no words;
   a code only the link box sends may use its group's fallback sentence (listed as a follow-up, not a failure).
   A new log code also needs an entry in `apps/add_backend/zk_add/activity_words_data.py`
   (`tests/unit/test_word_codes.py` checks it). A new generator domain needs an entry in `HAS_WORDS` in `words.test.ts`.

The backend has its own word modules for fields it sends: `be/problem_words.py` (`plain_title`, `plain_detail`,
`next_step`, the Problems page type map), `be/activity_words.py` (Activity items), `be/attendance_words.py`,
`be/device_names.py` (Life / Health and friendly names, also used by `/status`).
