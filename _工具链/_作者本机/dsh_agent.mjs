// QQ bot ↔ 本机 DSH 的桥接 agent（路线 A）
//
// 职责：每 2 秒通过 SSH 到服务器取一个任务，按**固定动作表**执行，把结果写回去。
// 它只做一件事：`dsh.run` —— 用 `dsh --profile headless "<任务>"` 跑一次。
//
// ## 为什么是「取件」而不是「被连」
// 服务器 sshd 是 `gatewayports no`，bot 容器又在自定义 bridge 网络里（够不到宿主 loopback），
// 所以「服务器主动连本机」这条路是堵的。反过来：本机是 SSH 客户端 —— 方向顺、不用开任何入站端口、
// 也不用改服务器任何配置。这是路线 A 的全部理由。
//
// ## 安全边界（不是"以后再加"，是设计前提）
// 1. **只做动作表里的动作**。表里目前只有 dsh.run；**永不执行任意 shell**。
//    服务器侧只写"数据"（一个 JSON），从来不发命令字符串。
// 2. **只认主人**：任务里 `from != "master"` 直接拒（bot 端已拦一层，这里再拦一层）。
// 3. **任务文本只作为一个 argv 元素**传给 dsh，不经 shell ——
//    所以任务里带引号、分号、管道都只是普通文字，不构成注入。
// 4. **单飞**：同一时刻只跑一个任务，避免被刷成一堆 DSH 进程。
// 5. **过期不捡**：任务文件超过 MAX_TASK_AGE 秒就丢弃，防止 agent 重启后捡到陈年任务。
// 6. **审计**：每条任务的处理过程写本地日志，谁发起、跑了什么、结果如何。
//
// 用法：
//   node dsh_agent.mjs --once        跑一轮就退出（验证通道用）
//   node dsh_agent.mjs --dry-run     只取件+校验，不真的执行（验证白名单用）
//   node dsh_agent.mjs               常驻轮询
//   node dsh_agent.mjs --interval 2  轮询间隔（秒）

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, appendFileSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

// ---------------------------------------------------------------- 配置
// 服务器地址与远端目录从环境变量取（默认值只是"照 deploy/README.md 部署"时的常见形态）。
// 用环境变量而不是写死：这两项属于个人环境，不该固化进仓库。
//     $env:QQBOT_SSH_HOST   = 'myserver'                  # ~/.ssh/config 里的别名
//     $env:QQBOT_REMOTE_DIR = '/opt/qq-bot/data/runtime/dsh_bridge'
const SSH_HOST = process.env.QQBOT_SSH_HOST || "qqbot";
const REMOTE_DIR = process.env.QQBOT_REMOTE_DIR || "/opt/qq-bot/data/runtime/dsh_bridge";
const INTERVAL_SEC = 2;                         // 轮询间隔（实测单次往返 ~800ms）
const MAX_TASK_AGE_SEC = 600;                   // 超过这么久没被取走的任务直接丢弃
const MAX_OUTPUT_BYTES = 4 * 1024 * 1024;       // 单次 dsh 输出上限（防内存被拉爆）
const MAX_TRANSPORT_CHARS = 4000;               // 回写结果的上限（超了截断并另存全文）
// dsh 可执行脚本的位置。npx 缓存的目录名随版本变化，找不到就显式指一个：
//     $env:QQBOT_DSH_PS1 = 'C:\path\to\dsh.ps1'
const DSH_PS1 = process.env.QQBOT_DSH_PS1 || join(
  homedir(),
  "AppData", "Local", "npm-cache", "_npx", "1e7f6d9597241db0",
  "node_modules", ".bin", "dsh.ps1",
);
const WORK_DIR = join(tmpdir(), "dsh_agent_work");
const LOG_FILE = join(WORK_DIR, "audit.log");

// ---------------------------------------------------------------- CLI
const argv = process.argv.slice(2);
const ONCE = argv.includes("--once");
const DRY_RUN = argv.includes("--dry-run");
const INTERVAL = (() => {
  const i = argv.indexOf("--interval");
  const v = i >= 0 ? Number(argv[i + 1]) : NaN;
  return Number.isFinite(v) && v > 0 ? v : INTERVAL_SEC;
})();

// ---------------------------------------------------------------- 工具
mkdirSync(WORK_DIR, { recursive: true });

function audit(msg) {
  const line = `${new Date().toISOString()} ${msg}\n`;
  try { appendFileSync(LOG_FILE, line, "utf8"); } catch { /* 审计失败不能拖垮主流程 */ }
  if (!ONCE) process.stdout.write(line);
}

function ssh(args, opts = {}) {
  const r = spawnSync("ssh", ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SSH_HOST, ...args], {
    encoding: "utf8", maxBuffer: 8 * 1024 * 1024, ...opts,
  });
  if (r.error) return { ok: false, out: "", err: String(r.error.message || r.error) };
  return { ok: r.status === 0, out: r.stdout || "", err: r.stderr || "" };
}

const sshRead = (remotePath) => ssh(["cat", remotePath]);
const sshRm = (remotePath) => ssh(["rm", "-f", remotePath]);

function writeResult(taskId, payload) {
  const local = join(WORK_DIR, `result-${taskId}.json`);
  writeFileSync(local, JSON.stringify(payload, null, 2), "utf8");
  const put = spawnSync("scp", ["-o", "BatchMode=yes", local, `${SSH_HOST}:${REMOTE_DIR}/out/`], {
    encoding: "utf8",
  });
  const ok = put.status === 0;
  if (!ok) audit(`RESULT-PUT-FAIL id=${taskId} ${put.stderr || ""}`);
  return ok;
}

/** 进度回写：让"任务在跑"这件事在服务器侧可见（否则 bot 只能干等） */
function writeProgress(taskId, note) {
  ssh([`printf '%s\\n' ${JSON.stringify(note)} > ${REMOTE_DIR}/out/progress-${taskId}.txt`]);
}

function stripAnsi(s) {
  return String(s || "").replace(/\u001b\[[0-9;]*[A-Za-z]/g, "");
}

// ---------------------------------------------------------------- 动作表
// 加动作必须**两边同时改**：这里 + 服务器侧 dsh_bridge.ALLOWED_ACTIONS。
const ACTIONS = {
  /**
   * 跑一次 DSH（headless 表层）。
   * 任务文本作为**单个 argv 元素**传入，不经 shell。
   */
  "dsh.run": async (task, ctx) => {
    if (!existsSync(DSH_PS1)) {
      return { status: "error", exit_code: null, stdout: "", stderr: `找不到 dsh 入口：${DSH_PS1}` };
    }
    const timeoutMs = Number(task.timeout_seconds || 300) * 1000;
    audit(`RUN id=${ctx.id} timeout=${timeoutMs / 1000}s task=${JSON.stringify(String(task.task).slice(0, 80))}`);
    writeProgress(ctx.id, `running: ${String(task.task).slice(0, 60)}`);

    return await new Promise((resolve) => {
      const child = spawn(
        "powershell",
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", DSH_PS1,
         "--profile", "headless", String(task.task)],
        { cwd: WORK_DIR, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] },
      );

      let out = "", err = "", outBytes = 0, killed = false;
      const cap = (s, chunk) => {
        outBytes += Buffer.byteLength(chunk);
        return outBytes > MAX_OUTPUT_BYTES ? s : s + chunk;
      };
      child.stdout.on("data", (d) => { out = cap(out, d.toString("utf8")); });
      child.stderr.on("data", (d) => { err = cap(err, d.toString("utf8")); });

      const timer = setTimeout(() => {
        killed = true;
        try { child.kill(); } catch { /* 已经退出 */ }
        audit(`TIMEOUT id=${ctx.id}`);
      }, timeoutMs);

      child.on("error", (e) => {
        clearTimeout(timer);
        resolve({ status: "error", exit_code: null, stdout: "", stderr: `spawn 失败：${e.message}` });
      });
      child.on("close", (code) => {
        clearTimeout(timer);
        resolve({
          status: killed ? "timeout" : code === 0 ? "ok" : "failed",
          exit_code: code,
          stdout: stripAnsi(out).trim(),
          stderr: stripAnsi(err).trim(),
        });
      });
    });
  },
};

// ---------------------------------------------------------------- 取件与处理
function listPending() {
  const r = ssh([`ls -1 ${REMOTE_DIR}/task-*.json 2>/dev/null`]);
  return (r.out || "").split("\n").map((s) => s.trim()).filter((s) => s.endsWith(".json"));
}

async function handleOne(remotePath) {
  const raw = sshRead(remotePath);
  if (!raw.ok) { audit(`READ-FAIL ${remotePath} ${raw.err.slice(0, 120)}`); return false; }

  let task;
  try {
    task = JSON.parse(raw.out);
  } catch (e) {
    audit(`BAD-JSON ${remotePath} ${e.message}`);
    writeResult(remotePath.match(/task-(.+)\.json/)?.[1] || "unknown",
      { status: "error", exit_code: null, stdout: "", stderr: "任务文件不是合法 JSON" });
    sshRm(remotePath);
    return true;
  }

  const id = String(task.id || "");
  const action = String(task.action || "");

  // ---- 校验（任何一条不过就丢弃，并留下审计）----
  if (String(task.from || "") !== "master") {
    audit(`REJECT id=${id} from=${task.from} （只认主人）`);
    writeResult(id, { id, status: "rejected", exit_code: null, stdout: "", stderr: "只有主人可以下发任务" });
    sshRm(remotePath);
    return true;
  }
  const ageSec = Math.floor(Date.now() / 1000) - Math.floor(new Date(String(task.created_at).replace(" ", "T") + "+08:00").getTime() / 1000);
  if (Number.isFinite(ageSec) && ageSec > MAX_TASK_AGE_SEC) {
    audit(`STALE id=${id} age=${ageSec}s （超过 ${MAX_TASK_AGE_SEC}s，丢弃）`);
    writeResult(id, { id, status: "stale", exit_code: null, stdout: "", stderr: `任务已过期（${ageSec}s）` });
    sshRm(remotePath);
    return true;
  }
  if (!Object.hasOwn(ACTIONS, action)) {
    audit(`REJECT id=${id} action=${action} （不在动作表里）`);
    writeResult(id, { id, status: "rejected", exit_code: null, stdout: "", stderr: `动作不在白名单：${action}` });
    sshRm(remotePath);
    return true;
  }

  // 取件即删除：避免同一个任务被执行两次
  sshRm(remotePath);

  if (DRY_RUN) {
    audit(`DRY-RUN id=${id} action=${action} task=${JSON.stringify(task.task)} —— 校验通过，未执行`);
    writeResult(id, { id, status: "dry-run", exit_code: null, stdout: `[dry-run] 校验通过，未执行：${task.task}`, stderr: "" });
    return true;
  }

  const result = await ACTIONS[action](task, { id });
  const full = { id, action, ...result, handled_by: "local-agent", stamp: new Date().toISOString() };

  // 结果太长就本地存全文，回写截断版（服务器侧还会再截一次转发给 QQ）
  let body = full.stdout;
  if (body.length > MAX_TRANSPORT_CHARS) {
    const dump = join(WORK_DIR, `stdout-${id}.txt`);
    writeFileSync(dump, full.stdout, "utf8");
    body = body.slice(0, MAX_TRANSPORT_CHARS) + `\n…（已截断，全文在 ${dump}）`;
  }
  writeResult(id, { ...full, stdout: body });
  audit(`DONE id=${id} status=${full.status} exit=${full.exit_code} out=${full.stdout.length}字`);
  return true;
}

// ---------------------------------------------------------------- 主循环
async function tick() {
  const pending = listPending();
  if (pending.length === 0) return 0;
  if (pending.length > 1) audit(`QUEUE-BACKLOG ${pending.length} 个任务待处理（单飞，逐个来）`);
  let n = 0;
  for (const p of pending) {           // 单飞：串行，不并发
    const handled = await handleOne(p);
    if (handled) n++;
  }
  return n;
}

audit(`AGENT-START once=${ONCE} dry_run=${DRY_RUN} interval=${INTERVAL}s host=${SSH_HOST}`);
audit(`SSH-PROBE ${ssh(["echo ok"]).ok ? "ok" : "FAIL"}`);

if (ONCE || DRY_RUN) {
  const n = await tick();
  audit(`AGENT-EXIT handled=${n}`);
  process.exit(0);
}

for (;;) {
  try { await tick(); } catch (e) { audit(`TICK-ERROR ${e && e.message}`); }
  await new Promise((r) => setTimeout(r, INTERVAL * 1000));
}
