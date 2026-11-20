<template>
  <section class="page" data-module="compare_job">
    <header class="page-head">
      <div>
        <h2>对比分析管理</h2>
        <p class="page-desc">维护基准数据与对比数据的差异分析任务和统计检验流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记对比任务</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出对比分析清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无对比分析数据，可先登记对比任务</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记对比任务</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="diff-panel" data-testid="diff-panel">
      <header class="diff-head">
        <div>
          <h3>归因对比差异清单</h3>
          <p class="page-desc">
            两侧金额与差异统一按 <strong data-testid="diff-precision">{{ diffPrecision }}</strong>
            位小数发布；差异等于两侧已发布金额之差的绝对值，可用表中两个金额直接复算。
          </p>
        </div>
        <div class="diff-actions">
          <label class="diff-inline">
            <span>链路编号</span>
            <input v-model="diffLink" data-testid="diff-link" placeholder="如 ATTR-PRECISION" />
          </label>
          <label class="diff-inline diff-check">
            <input v-model="diffLateBatch" data-testid="diff-late" type="checkbox" />
            <span>并入迟到补报</span>
          </label>
          <button class="btn" data-testid="run-attribution-diff" type="button" :disabled="diffRunning" @click="runAttributionDiff">
            {{ diffRunning ? '计算中…' : '执行归因对比' }}
          </button>
        </div>
      </header>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">比较覆盖触点数</span>
          <strong class="stat-value" data-testid="diff-covered">{{ diffCoverage.比较覆盖触点数 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已确认差异触点数</span>
          <strong class="stat-value" data-testid="diff-confirmed">{{ diffCoverage.已确认差异触点数 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已核平触点数</span>
          <strong class="stat-value" data-testid="diff-reconciled">{{ diffCoverage.已核平触点数 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">单侧待补触点数</span>
          <strong class="stat-value" data-testid="diff-pending">{{ diffCoverage.单侧待补触点数 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">按分差异合计</span>
          <strong class="stat-value" data-testid="diff-total">{{ diffTotal }}</strong>
        </article>
      </div>

      <table class="data-table diff-table">
        <thead>
          <tr>
            <th>触点类型</th>
            <th>首次归因</th>
            <th>末次归因</th>
            <th>差异</th>
            <th>可比状态</th>
            <th>已确认差异</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in diffRows"
            :key="String(row['触点类型'])"
            :data-testid="`diff-row-${String(row['触点类型'])}`"
            :data-status="String(row['可比状态'])"
          >
            <td>{{ row['触点类型'] }}</td>
            <td>{{ formatAmount(row['首次归因']) }}</td>
            <td>{{ formatAmount(row['末次归因']) }}</td>
            <td>{{ formatAmount(row['差异']) }}</td>
            <td>{{ row['可比状态'] }}</td>
            <td>{{ row['已确认差异'] ? '是' : '否' }}</td>
          </tr>
          <tr v-if="!diffRows.length">
            <td colspan="6" class="empty-state" data-testid="diff-empty">{{ diffEmptyReason }}</td>
          </tr>
        </tbody>
      </table>

      <header class="diff-head claim-head">
        <div>
          <h3>本班可销账清单</h3>
          <p class="page-desc">
            只有两侧都能核对到分的触点才进入销账范围；已核平的触点可尽早关掉，
            补数后金额变化的触点需要重新核对，已销账的不会重复销账。
          </p>
        </div>
        <span class="diff-batch" data-testid="claim-batch">批次：{{ claimBatch || '—' }}</span>
      </header>

      <table class="data-table">
        <thead>
          <tr>
            <th>触点类型</th>
            <th>首次归因</th>
            <th>末次归因</th>
            <th>差异</th>
            <th>核对状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in claimItems"
            :key="String(item['触点类型'])"
            :data-testid="`claim-row-${String(item['触点类型'])}`"
          >
            <td>{{ item['触点类型'] }}</td>
            <td>{{ formatAmount(item['首次归因']) }}</td>
            <td>{{ formatAmount(item['末次归因']) }}</td>
            <td>{{ formatAmount(item['差异']) }}</td>
            <td>
              <span v-if="item['需复核']" class="badge warn">需复核</span>
              <span v-else-if="item['已销账']" class="badge ok">已销账</span>
              <span v-else-if="item['已核平']" class="badge ok">已核平</span>
              <span v-else class="badge">待销账</span>
            </td>
            <td>
              <button
                v-if="!item['已销账']"
                class="link"
                type="button"
                :data-testid="`claim-${String(item['触点类型'])}`"
                @click="settleOne(String(item['触点类型']))"
              >
                {{ item['需复核'] ? '重新核对并销账' : '销账' }}
              </button>
              <span v-else class="muted">—</span>
            </td>
          </tr>
          <tr v-if="!claimItems.length">
            <td colspan="6" class="empty-state" data-testid="claim-empty">{{ claimEmptyReason }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条对比分析记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/compare_job'
const columns = ["任务编号", "基准数据", "对比数据", "比对字段", "差异阈值", "对比周期", "差异行数", "处理结论"]
const actions = ["执行对比", "标记完成", "标记差异"]
const statuses = ["待执行", "对比中", "已完成", "差异显著"]
const metricNames = ["任务总数", "对比中任务", "差异显著任务"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
type DiffRow = Record<string, string | number | boolean | null>
type ClaimItem = Record<string, string | number | boolean | null>

const diffLink = ref('ATTR-PRECISION')
const diffLateBatch = ref(false)
const diffRows = ref<DiffRow[]>([])
const diffPrecision = ref(2)
const diffBatch = ref('')
const diffRunning = ref(false)
const diffCoverage = ref({ 比较覆盖触点数: 0, 已确认差异触点数: 0, 已核平触点数: 0, 单侧待补触点数: 0 })
const diffEmptyReason = ref('')
const claimItems = ref<ClaimItem[]>([])
const claimBatch = ref('')
const claimEmptyReason = ref('')

// 合计用整数分累加，避免浮点相加出现 0.7800000000000001。
const diffTotal = computed(() => {
  const factor = 10 ** diffPrecision.value
  const cents = diffRows.value
    .filter((row) => row['已确认差异'])
    .reduce((sum, row) => sum + Math.round(Number(row['差异'] ?? 0) * factor), 0)
  return (cents / factor).toFixed(diffPrecision.value)
})

function formatAmount(value: unknown) {
  if (value === null || value === undefined) return '未取得'
  return Number(value).toFixed(diffPrecision.value)
}

async function loadDiff() {
  errorMessage.value = ''
  const query = new URLSearchParams({ link_code: diffLink.value }).toString()
  try {
    const response = await request(`${ENDPOINT}/diff-rows?${query}`)
    if (!response.ok) throw new Error('差异清单读取失败')
    const payload = await response.json()
    diffRows.value = payload.items ?? []
    diffPrecision.value = payload.precision ?? 2
    diffBatch.value = payload.batch_id ?? ''
    diffCoverage.value = payload.coverage ?? diffCoverage.value
    diffEmptyReason.value = payload.empty_reason ?? '暂无可比较的差异行'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '差异清单读取失败'
  }
}

async function loadClaimable() {
  const query = new URLSearchParams({ link_code: diffLink.value }).toString()
  try {
    const response = await request(`${ENDPOINT}/claimable?${query}`)
    if (!response.ok) throw new Error('可销账清单读取失败')
    const payload = await response.json()
    claimItems.value = payload.items ?? []
    claimBatch.value = payload.batch_id ?? ''
    claimEmptyReason.value = payload.empty_reason ?? '本班暂无可领项目'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '可销账清单读取失败'
  }
}

async function refreshDiffPanels() {
  await loadDiff()
  await loadClaimable()
}

async function runAttributionDiff() {
  diffRunning.value = true
  errorMessage.value = ''
  try {
    const params = new URLSearchParams({
      link_code: diffLink.value,
      late_batch: String(diffLateBatch.value),
    })
    // 同一流水号重复提交：补齐后再次提交原批，只覆盖同一批差异行。
    if (diffBatch.value) params.set('batch_id', diffBatch.value)
    const response = await request(`/api/pipeline/attribution-compare?${params.toString()}`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '归因对比未执行')
    await refreshDiffPanels()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归因对比未执行'
  } finally {
    diffRunning.value = false
  }
}

async function settleOne(touchType: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/settle`, {
      method: 'POST',
      body: JSON.stringify({
        触点类型: [touchType],
        batch_id: claimBatch.value || undefined,
        link_code: diffLink.value || undefined,
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '销账失败')
    await refreshDiffPanels()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '销账失败'
  }
}

watch(diffLink, () => {
  void refreshDiffPanels()
})

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'compare_job.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('对比分析动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对比分析操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('对比任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对比分析列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await refreshDiffPanels()
})
</script>
