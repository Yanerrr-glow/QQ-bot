// DSH 桥接 agent 的守护/自启包装
//
// 三件事：单实例守卫、崩溃自动重启、开机自启的安装/卸载。
// 为什么单独一个文件而不是把逻辑塞进 dsh_agent.mjs：agent 本身要能在任何环境
// 用 `node dsh_agent.mjs --once` 单跑（排查时最有用），守护是"常驻"才需要的东西。
//
// 用法：
//   node dsh_agent_daemon.mjs              前台守护（Ctrl+C 退出）
//   node dsh_agent_daemon.mjs --install    注册开机自启（登录时启动）+ 立即启动
//   node dsh_agent_daemon.mjs --uninstall  移除开机自启
//   node dsh_agent_daemon.mjs --status     看是否在跑
//
// 日志：%TEMP%\dsh_agent_work\daemon.log（agent 自己的 stdout/stderr 汇到这里）

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync, rmSync, appendFileSync, openSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const AGENT = join(HERE, "dsh_agent.mjs");
const WORK = join(tmpdir(), "dsh_agent_work");
const LOG = join(WORK, "daemon.log");
const PID_FILE = join(WORK, "daemon.pid");
const TASK_NAME = "DSH-Bridge-Agent";

mkdirSync(WORK, { recursive: true });

const argv = process.argv.slice(2);
const has = (f) => argv.includes(f);

function log(msg) {
  const line = `${new Date().toISOString()} [daemon] ${msg}\n`;
  try { appendFileSync(LOG, line, "utf8"); } catch { /* 日志失败不致命 */ }
  process.stdout.write(line);
}

function readPid() {
  try { return Number(readFileSync(PID_FILE, "utf8").trim()) || 0; } catch { return 0; }
}

/** 这个 pid 还活着吗（Windows 上用 tasklist 探，避免 process.kill 的权限噪音） */
function pidAlive(pid) {
  if (!pid) return false;
  const r = spawnSync("tasklist", ["/FI", `PID eq ${pid}`, "/NH"], { encoding: "utf8" });
  return String(r.stdout || "").includes(String(pid));
}

function startAgent() {
  if (!existsSync(AGENT)) {
    log(`[x] 找不到 agent：${AGENT}`);
    process.exit(1);
  }
  const out = openSync(LOG, "a");   // 追加：重启用同一个日志文件，方便回看
  const child = spawn(process.execPath, [AGENT], {
    cwd: WORK,
    detached: true,                 // 脱离父进程：装完自启后本进程可以退出
    stdio: ["ignore", out, out],
    windowsHide: true,
  });
  child.unref();
  writeFileSync(PID_FILE, String(child.pid), "utf8");
  log(`agent 已启动 pid=${child.pid}（日志 ${LOG}）`);
  return child.pid;
}

// ---------------------------------------------------------------- 子命令
if (has("--status")) {
  const pid = readPid();
  const r = spawnSync("schtasks", ["/Query", "/TN", TASK_NAME], { encoding: "utf8" });
  log(`agent pid=${pid || "(无)"} 存活=${pidAlive(pid)}  自启任务=${r.status === 0 ? "已注册" : "未注册"}`);
  process.exit(0);
}

if (has("--uninstall")) {
  const r = spawnSync("schtasks", ["/Delete", "/TN", TASK_NAME, "/F"], { encoding: "utf8" });
  log(r.status === 0 ? "自启任务已移除" : `移除失败（可能本来就没有）：${r.stderr || r.stdout}`);
  const pid = readPid();
  if (pidAlive(pid)) {
    spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { encoding: "utf8" });
    log(`已停止正在运行的 agent pid=${pid}`);
  }
  try { rmSync(PID_FILE); } catch { /* 没就没 */ }
  process.exit(0);
}

if (has("--install")) {
  // **由守护托管，而不是起裸 agent**：裸 agent 崩了没人拉起来。
  // 顺序：先把守护挂到后台（它负责起 agent + 崩了重启），再注册登录自启。
  const old = readPid();
  if (pidAlive(old)) {
    log(`已有 agent 在跑（pid=${old}），跳过启动`);
  } else {
    const out = openSync(LOG, "a");
    const d = spawn(process.execPath, [join(HERE, "dsh_agent_daemon.mjs"), "--foreground"], {
      cwd: WORK, detached: true, stdio: ["ignore", out, out], windowsHide: true,
    });
    d.unref();
    log(`守护已在后台启动 pid=${d.pid}（它负责起 agent，并在 agent 崩溃时 3 秒后重启）`);
  }

  const cmd = `"${process.execPath}" "${join(HERE, "dsh_agent_daemon.mjs")}" "--foreground"`;
  const r = spawnSync(
    "schtasks",
    ["/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED", "/F", "/TR", cmd],
    { encoding: "utf8" },
  );
  if (r.status === 0) {
    log(`已注册开机自启：${TASK_NAME}（登录时启动守护）`);
    log(`  命令：${cmd}`);
  } else {
    log(`[x] 注册自启失败：${(r.stderr || r.stdout || "").trim().slice(0, 200)}`);
    log("    可手动执行（管理员 PowerShell）：schtasks /Create /TN " + TASK_NAME +
        " /SC ONLOGON /TR \"" + cmd + "\" /F");
  }
  log("完成。用 --status 看状态、--uninstall 撤销。");
  process.exit(0);
}

// 后台守护：不占窗口，给自启/托管用（日志都在 daemon.log）
if (has("--detach")) {
  const existingPid = readPid();
  if (pidAlive(existingPid)) {
    log(`已有 agent 在跑（pid=${existingPid}），不重复启动`);
    process.exit(0);
  }
  const out = openSync(LOG, "a");
  const child = spawn(process.execPath, [join(HERE, "dsh_agent_daemon.mjs")], {
    cwd: WORK, detached: true, stdio: ["ignore", out, out], windowsHide: true,
  });
  child.unref();
  log(`守护已在后台启动 pid=${child.pid}`);
  process.exit(0);
}

// ---------------------------------------------------------------- 前台守护
const existing = readPid();
if (pidAlive(existing) && existing !== process.pid) {
  log(`已有 agent 在跑（pid=${existing}）。同一时刻只允许一个（任务只能被执行一次）。`);
  process.exit(1);
}

log(`前台守护启动；agent 崩溃会自动重启（Ctrl+C 退出）`);
let child = null;
let stopping = false;

function boot() {
  if (stopping) return;
  const out = openSync(LOG, "a");
  child = spawn(process.execPath, [AGENT], { cwd: WORK, stdio: ["ignore", out, out], windowsHide: true });
  writeFileSync(PID_FILE, String(child.pid), "utf8");
  log(`agent 启动 pid=${child.pid}`);
  child.on("exit", (code) => {
    if (stopping) return;
    log(`agent 退出（code=${code}），3 秒后重启`);
    setTimeout(boot, 3000);
  });
}

process.on("SIGINT", () => {
  stopping = true;
  log("收到 Ctrl+C，停止 agent 并退出");
  if (child) { try { child.kill(); } catch { /* 已退 */ } }
  try { rmSync(PID_FILE); } catch { /* 没就没 */ }
  process.exit(0);
});

boot();
