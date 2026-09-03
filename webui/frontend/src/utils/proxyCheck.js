// 代理池快速评估：抽样 + 延迟汇总。
// 代理池存在浏览器 localStorage（stores/proxy.js），后端不持有池，所以抽样只能在前端做；
// 抽完样再走已有的 POST /api/proxy/test 批量测。
// 这里只放纯函数，方便脱离 Vue 直接跑断言验证。

export const SAMPLE_RATIO = 0.1
// 下限 5：池子只有十几个时按 10% 只能抽 1-2 个，一个超时代理就让可用率在 0%/100% 之间跳，没有参考价值。
// 上限 30：后端并发 20、单个默认超时 8s，30 个最坏约 16s；再多就不是「快速」评估了。
export const SAMPLE_MIN = 5
export const SAMPLE_MAX = 30

/**
 * 从代理池里随机抽一批做评估。
 * 取 ceil(总数 × ratio) 再夹到 [min, max]；池子本身不足 min 个时全测。
 * 用部分 Fisher-Yates 洗牌，保证每个代理被抽中的概率相同，且不修改传入数组。
 * @returns {string[]} 抽中的代理，顺序随机
 */
export function sampleProxies(list, options = {}) {
  const { ratio = SAMPLE_RATIO, min = SAMPLE_MIN, max = SAMPLE_MAX } = options
  const pool = [...(list || [])]
  if (!pool.length) return []

  const wanted = Math.ceil(pool.length * ratio)
  const size = Math.min(pool.length, Math.max(min, Math.min(max, wanted)))

  for (let i = 0; i < size; i += 1) {
    const j = i + Math.floor(Math.random() * (pool.length - i))
    ;[pool[i], pool[j]] = [pool[j], pool[i]]
  }
  return pool.slice(0, size)
}

/** nearest-rank 百分位：小样本下取值确定，不做插值，返回的一定是真实观测到的延迟。 */
function percentile(sortedValues, p) {
  if (!sortedValues.length) return 0
  const rank = Math.ceil((p / 100) * sortedValues.length)
  return sortedValues[Math.min(sortedValues.length - 1, Math.max(0, rank - 1))]
}

function median(sortedValues) {
  if (!sortedValues.length) return 0
  const mid = Math.floor(sortedValues.length / 2)
  if (sortedValues.length % 2) return sortedValues[mid]
  return Math.round((sortedValues[mid - 1] + sortedValues[mid]) / 2)
}

/**
 * 汇总一次抽样测试的结果。
 * 延迟指标只统计成功的代理：失败项的 latency_ms 其实是「超时/握手失败前耗掉的时间」，
 * 混进分位数会把中位数拉到超时值上，看着像全池都慢。失败情况由可用率和失败数单独体现。
 * @param {Array<{proxy:string, ok:boolean, latency_ms:number}>} results 抽样代理的测试结果
 * @param {number} total 代理池总数，用于展示「样本 N/总数」
 */
export function summarizeLatency(results, total = 0) {
  const rows = results || []
  const okRows = rows.filter((r) => r && r.ok)
  const latencies = okRows
    .map((r) => Number(r.latency_ms) || 0)
    .sort((a, b) => a - b)

  const slowest = okRows.length
    ? okRows.reduce((worst, r) =>
        (Number(r.latency_ms) || 0) > (Number(worst.latency_ms) || 0) ? r : worst)
    : null

  return {
    sampled: rows.length,
    total: total || rows.length,
    okCount: okRows.length,
    failCount: rows.length - okRows.length,
    // 无样本时记 0 而不是 NaN，避免界面显示 "NaN%"
    availability: rows.length ? Math.round((okRows.length / rows.length) * 100) : 0,
    medianMs: median(latencies),
    p95Ms: percentile(latencies, 95),
    slowestProxy: slowest ? slowest.proxy : '',
    slowestMs: slowest ? Number(slowest.latency_ms) || 0 : 0,
  }
}
