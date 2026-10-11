// runtime/_common.js —— 测试脚本共用的路径 / 环境解析
//
// 约定：仓库里不出现任何「属于本机某次检出」的绝对路径。
//   - RUNTIME / ROOT 由本文件自身位置推算，换机器、换目录、换盘符都能用
//   - Chrome 可执行文件优先读环境变量 CHROME_PATH，其次探测常见安装位置
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

/** <项目>/runtime —— 测试脚本自己待的目录 */
export const RUNTIME = HERE

/** <项目> 根目录 */
export const ROOT = dirname(HERE)

/** 本机 Chrome 可执行文件（找不到时进程直接退出并给出提示） */
export const CHROME = resolveChrome()

function resolveChrome() {
  // 用环境变量拼常见安装位置，避免把某个盘符写死在代码里
  const winRoots = [
    process.env.PROGRAMFILES,
    process.env['PROGRAMFILES(X86)'],
    process.env.LOCALAPPDATA,
  ].filter(Boolean)
  const windows = winRoots.map((r) =>
    join(r, 'Google', 'Chrome', 'Application', 'chrome.exe'),
  )
  const unix = [
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ]

  const candidates = [process.env.CHROME_PATH, ...windows, ...unix].filter(Boolean)
  const hit = candidates.find((p) => existsSync(p))

  if (!hit) {
    console.error(
      '[runtime] 找不到 Chrome。请设置环境变量 CHROME_PATH 指向 Chrome 可执行文件，例如：\n' +
        '  set CHROME_PATH=<chrome.exe 的完整路径>',
    )
    process.exit(1)
  }
  return hit
}
