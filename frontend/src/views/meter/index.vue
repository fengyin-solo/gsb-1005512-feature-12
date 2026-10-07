<template>
  <section class="page" data-module="meter">
    <header class="page-head">
      <div>
        <h2>关口计量管理</h2>
        <p class="page-desc">维护关口表计，围绕表计编号、计量点名称、表计精度、正向有功电量做登记、筛选与状态流转。支持抄表文件整批导入（列：表计编号,抄表日期,上月示数,本月示数,正向有功电量,反向有功电量），同一计量点同一月重复导入以最后一次为准。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记关口表计</button>
        <button class="btn" type="button" @click="downloadTemplate">下载导入模板</button>
        <button class="btn" type="button" @click="pickFile">导入抄表文件</button>
        <button v-if="failedBatch" class="btn" type="button" @click="retryImport">重试导入（最后一次）</button>
        <button class="btn" type="button" @click="exportRows">导出关口计量清单</button>
        <input ref="fileInput" type="file" accept=".csv,.txt" class="hidden-input" @change="onFilePicked" />
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
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      <label class="filter-item">
        <span>结算月份</span>
        <input v-model="settlementMonth" type="month" />
      </label>
      <button class="btn" type="button" @click="exportSettlement">导出结算对账文件</button>
    </form>

    <div v-if="importMessage" class="import-result">
      <p :class="importOk ? 'ok-text' : 'error-text'">{{ importMessage }}</p>
      <ul v-if="rejected.length" class="rejected-list">
        <li v-for="item in rejected" :key="item.row">第 {{ item.row }} 行（{{ item.表计编号 }}）：{{ item.reason }}</li>
      </ul>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="row.status === '通讯中断'"
              class="link"
              type="button"
              @click="fetchReading(row)"
            >
              重取示数
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无关口计量数据，可先登记关口表计</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条关口计量记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type RejectedRow = { row: number; 表计编号: string; reason: string }

const ENDPOINT = '/api/meter'
const columns = ["表计编号", "计量点名称", "表计精度", "正向有功电量", "反向有功电量", "上月示数", "本月示数", "通讯状态"]
const actions = ["确认正常", "标记异常", "停用表计"]
const statuses = ["通讯正常", "数据异常", "通讯中断", "已停用"]
const stats = [{"label": "日发电量", "value": 0}, {"label": "日上网电量", "value": 0}, {"label": "异常表计数", "value": 0}]
const IMPORT_HEADERS = ["表计编号", "抄表日期", "上月示数", "本月示数", "正向有功电量", "反向有功电量"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const settlementMonth = ref('')

const fileInput = ref<HTMLInputElement | null>(null)
const importMessage = ref('')
const importOk = ref(false)
const rejected = ref<RejectedRow[]>([])
const failedBatch = ref<{ batchId: string; rows: Array<Record<string, string>> } | null>(null)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function exportSettlement() {
  const query = settlementMonth.value ? `?month=${settlementMonth.value}` : ''
  window.open(`${ENDPOINT}/settlement/export${query}`, '_blank')
}

function openCreate() {
  errorMessage.value = '关口表计登记入口尚未接入审批流'
}

function downloadTemplate() {
  const csv = `\ufeff${IMPORT_HEADERS.join(',')}\nMETE-0001,2026-10-01,1000.00,1123.45,,\n`
  const link = document.createElement('a')
  link.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  link.download = '抄表导入模板.csv'
  link.click()
  URL.revokeObjectURL(link.href)
}

function pickFile() {
  fileInput.value?.click()
}

async function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  const text = await file.text()
  const parsed = parseCsv(text)
  const batchId = `batch-${Date.now()}`
  await doImport(batchId, parsed)
}

function parseCsv(text: string): Array<Record<string, string>> {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  if (lines.length < 2) return []
  const headers = lines[0].replace(/^\ufeff/, '').split(',').map((cell) => cell.trim())
  return lines.slice(1).map((line) => {
    const cells = line.split(',')
    const row: Record<string, string> = {}
    headers.forEach((header, index) => {
      row[header] = (cells[index] ?? '').trim()
    })
    return row
  })
}

async function doImport(batchId: string, parsed: Array<Record<string, string>>) {
  errorMessage.value = ''
  importMessage.value = ''
  rejected.value = []
  try {
    const response = await request(`${ENDPOINT}/import`, {
      method: 'POST',
      body: JSON.stringify({ batch_id: batchId, rows: parsed }),
    })
    const result = await response.json()
    importOk.value = Boolean(result.ok)
    importMessage.value = result.message ?? '导入结束'
    rejected.value = result.rejected ?? []
    failedBatch.value = !result.ok && result.retryable ? { batchId, rows: parsed } : null
    await reload()
  } catch (error) {
    importOk.value = false
    importMessage.value = error instanceof Error ? error.message : '抄表文件导入失败'
    failedBatch.value = { batchId, rows: parsed }
  }
}

async function retryImport() {
  if (!failedBatch.value) return
  await doImport(failedBatch.value.batchId, failedBatch.value.rows)
}

async function fetchReading(row: Row) {
  errorMessage.value = ''
  importMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/fetch-reading`, { method: 'POST' })
    const result = await response.json()
    importOk.value = Boolean(result.ok)
    importMessage.value = result.message ?? '重取示数结束'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重取示数失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('关口计量动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '关口计量操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('关口表计列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '关口计量列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.hidden-input {
  display: none;
}

.import-result {
  margin: 8px 0;
  padding: 8px 12px;
  border: 1px solid var(--border-color, #d0d7de);
  border-radius: 6px;
}

.ok-text {
  color: #1a7f37;
}

.rejected-list {
  margin: 6px 0 0;
  padding-left: 18px;
  color: #b42318;
}
</style>
