import { closeSync, openSync, readSync, readdirSync, statSync, type Stats } from 'node:fs'
import { basename, dirname, join } from 'node:path'

const TRANSCRIPT_READ_MAX_BYTES = 1024 * 1024
const TRANSCRIPT_LINE_MAX_BYTES = 256 * 1024
const TRANSCRIPT_DIRECTORY_MAX_ENTRIES = 4096
export const SAFE_THREAD_ID = /^[A-Za-z0-9-]{1,64}$/

export type JsonlCursor = {
  filePath?: string
  offset: number
  carry: string
}

export type JsonRecord = Record<string, unknown>

export function record(value: unknown): JsonRecord | undefined {
  return typeof value === 'object' && value !== null ? (value as JsonRecord) : undefined
}

/** Returns undefined when the file is unreadable, distinguishing a vanished rollout from one with no new lines. */
export function readJsonlCursor(cursor: JsonlCursor): JsonRecord[] | undefined {
  if (!cursor.filePath) {
    return undefined
  }
  let stats: Stats
  try {
    stats = statSync(cursor.filePath)
  } catch {
    return undefined
  }
  if (!stats.isFile()) {
    return undefined
  }
  if (stats.size < cursor.offset) {
    cursor.offset = 0
    cursor.carry = ''
  }
  if (stats.size === cursor.offset) {
    return []
  }
  const bytesToRead = Math.min(stats.size - cursor.offset, TRANSCRIPT_READ_MAX_BYTES)
  const start = stats.size - cursor.offset > bytesToRead ? stats.size - bytesToRead : cursor.offset
  const buffer = Buffer.allocUnsafe(bytesToRead)
  let bytesRead = 0
  let fd: number | undefined
  try {
    fd = openSync(cursor.filePath, 'r')
    bytesRead = readSync(fd, buffer, 0, bytesToRead, start)
  } catch {
    return undefined
  } finally {
    if (fd !== undefined) {
      closeSync(fd)
    }
  }
  const skippedPrefix = start !== cursor.offset
  const content = `${skippedPrefix ? '' : cursor.carry}${buffer.toString('utf8', 0, bytesRead)}`
  const lines = content.split('\n')
  cursor.offset = start + bytesRead
  cursor.carry = lines.pop() ?? ''
  if (skippedPrefix) {
    lines.shift()
  }
  const records: JsonRecord[] = []
  for (const line of lines) {
    if (Buffer.byteLength(line, 'utf8') > TRANSCRIPT_LINE_MAX_BYTES) {
      continue
    }
    try {
      const parsed = record(JSON.parse(line) as unknown)
      if (parsed) {
        records.push(parsed)
      }
    } catch {
      // A malformed rollout line must not block later lifecycle events.
    }
  }
  return records
}

function readTranscriptDirectory(directory: string): string[] {
  let entries: string[]
  try {
    entries = readdirSync(directory)
  } catch {
    return []
  }
  if (entries.length > TRANSCRIPT_DIRECTORY_MAX_ENTRIES) {
    entries = entries.slice(-TRANSCRIPT_DIRECTORY_MAX_ENTRIES)
  }
  return entries
}

// Why: Codex files each rollout under its OWN local start date, so a session running past midnight spawns children into a sibling day directory.
function childDayDirectory(parentPath: string, startedAt: number): string | undefined {
  const dayDir = dirname(parentPath)
  const monthDir = dirname(dayDir)
  const yearDir = dirname(monthDir)
  if (
    !/^\d{2}$/.test(basename(dayDir)) ||
    !/^\d{2}$/.test(basename(monthDir)) ||
    !/^\d{4}$/.test(basename(yearDir)) ||
    !Number.isFinite(startedAt)
  ) {
    return undefined
  }
  const startedOn = new Date(startedAt)
  if (Number.isNaN(startedOn.getTime())) {
    return undefined
  }
  const pad = (value: number): string => String(value).padStart(2, '0')
  return join(
    dirname(yearDir),
    String(startedOn.getFullYear()).padStart(4, '0'),
    pad(startedOn.getMonth() + 1),
    pad(startedOn.getDate())
  )
}

export function resolveChildTranscript(
  parentPath: string,
  threadId: string,
  startedAt: number,
  entriesByDirectory: Map<string, string[]>
): string | undefined {
  if (!SAFE_THREAD_ID.test(threadId)) {
    return undefined
  }
  const suffix = `-${threadId}.jsonl`
  const parentDir = dirname(parentPath)
  const childDir = childDayDirectory(parentPath, startedAt)
  const directories = childDir && childDir !== parentDir ? [parentDir, childDir] : [parentDir]
  for (const directory of directories) {
    let entries = entriesByDirectory.get(directory)
    if (!entries) {
      entries = readTranscriptDirectory(directory)
      entriesByDirectory.set(directory, entries)
    }
    const fileName = entries.find((entry) => entry.endsWith(suffix))
    if (fileName) {
      return join(directory, fileName)
    }
  }
  return undefined
}
