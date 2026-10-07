<template>
  <section class="page" data-module="meter">
    <header class="page-head">
      <div>
        <h2>关口计量管理</h2>
        <p class="page-desc">抄表文件按表计编号一次性导入，同一计量点同月重复导入以最后一次为准；结算电量导出与列表、明细同一份口径。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="downloadTemplate">下载导入模板</button>
        <button class="btn primary" type="button" @click="triggerImport">导入抄表文件</button>
        <input
          ref="fileInput"
          type="file"
          accept=".csv,text/csv"
          hidden
          @change="onFilePicked"
        />
        <button class="btn" type="button" @click="exportRows('csv')">导出对账文件(CSV)</button>
        <button class="btn" type="button" @click="exportRows('json')">导出对账数据(JSON)</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>抄表月份</span>
        <input v-model="month" type="month" placeholder="YYYY-MM" />
      </label>
      <label class="filter-item">
        <span>表计编号</span>
        <input v-model="keyword" placeholder="按表计编号检索" />
      </label>
      <label class="filter-item">
        <span>通讯状态</span>
        <select v-model="status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] === '' || row[column] == null ? '—' : row[column] }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="showDetail(row)">示数明细</button>
            <button
              v-if="row['通讯状态'] === '通讯中断' || !row['本月示数']"
              class="link"
              type="button"
              @click="refetch(row)"
            >
              重新取数
            </button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">该月份暂无抄表示数，可先导入抄表文件</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条关口计量记录 · 当前口径：{{ month ? month + ' 月结' : '各表最后一次导入值' }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 导入结果：成功行与打回行（含原因）分开列出；整体失败时可就地重试一次 -->
    <div v-if="importResult" class="modal-mask" @click.self="importResult = null">
      <div class="modal">
        <h3>抄表文件导入结果</h3>
        <template v-if="importResult.fatal">
          <p class="error-text">文件未能导入：{{ importResult.fatal }}<template v-if="importResult.retried">（已自动重试一次）</template></p>
          <p class="modal-hint">同一计量点同月导入为整笔覆盖，重试不会产生两笔数据。</p>
          <div class="modal-actions">
            <button v-if="!importResult.retried" class="btn primary" type="button" :disabled="importing" @click="retryImport">
              {{ importing ? '重试中…' : '重试一次' }}
            </button>
            <button class="btn" type="button" @click="importResult = null">关闭</button>
          </div>
        </template>
        <template v-else>
          <p>
            共 {{ importResult.total }} 行：成功
            <strong>{{ importResult.imported_count }}</strong> 条，打回
            <strong :class="{ 'error-text': importResult.rejected_count > 0 }">{{ importResult.rejected_count }}</strong> 条
            <template v-if="importResult.retried">（重试后结果）</template>
          </p>
          <div v-if="importResult.imported.length" class="result-block">
            <h4>已入库（同月重复导入已整笔覆盖）</h4>
            <ul class="result-list">
              <li v-for="line in importResult.imported" :key="`in-${line.行号}`">
                第 {{ line.行号 }} 行 · {{ line.表计编号 }} · {{ line.月份 }}<span v-if="line.覆盖">（覆盖旧值）</span>
              </li>
            </ul>
          </div>
          <div v-if="importResult.rejected.length" class="result-block">
            <h4>打回行（未入库）</h4>
            <ul class="result-list">
              <li v-for="line in importResult.rejected" :key="`rj-${line.行号}`" class="error-text">
                第 {{ line.行号 }} 行<template v-if="line.表计编号"> · {{ line.表计编号 }}</template>：{{ line.原因 }}
              </li>
            </ul>
          </div>
          <div class="modal-actions">
            <button class="btn primary" type="button" @click="importResult = null">知道了</button>
          </div>
        </template>
      </div>
    </div>

    <!-- 单表历次示数：重进页面看到的仍是最后一次导入值 -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal modal-wide">
        <h3>示数明细 · {{ detail.表计编号 }}（{{ detail.计量点名称 }}）</h3>
        <p class="modal-hint">表计精度 {{ detail.表计精度 }} · 当前列表展示值即下方最近一期，导出对账与此同源。</p>
        <table class="data-table">
          <thead>
            <tr>
              <th v-for="column in readingColumns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in detail.示数明细" :key="`${r.月份}-${r.version}`">
              <td v-for="column in readingColumns" :key="column">{{ r[column] ?? '—' }}</td>
            </tr>
            <tr v-if="!detail.示数明细 || !detail.示数明细.length">
              <td :colspan="readingColumns.length" class="empty-state">暂无抄表示数</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface ImportLine {
  行号: number
  表计编号?: string | null
  月份?: string | null
  覆盖?: boolean | null
  原因?: string | null
}

interface ImportResult {
  ok: boolean
  total: number
  imported_count: number
  rejected_count: number
  imported: ImportLine[]
  rejected: ImportLine[]
  fatal?: string | null
  retried?: boolean
}

const ENDPOINT = '/api/meter'
const columns = ['表计编号', '计量点名称', '表计精度', '正向有功电量', '反向有功电量', '上月示数', '本月示数', '月份', '抄表日期', '通讯状态', '数据来源']
const readingColumns = ['月份', '抄表日期', '上月示数', '本月示数', '正向有功电量', '反向有功电量', '数据来源', '备注']
const actions = ['确认正常', '标记异常', '停用表计']
const statuses = ['通讯正常', '数据异常', '通讯中断', '已停用']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const month = ref('')
const keyword = ref('')
const status = ref('')

const stats = ref([
  { label: '正向有功电量合计', value: '—' },
  { label: '反向有功电量合计', value: '—' },
  { label: '异常表计数', value: 0 },
])

const fileInput = ref<HTMLInputElement | null>(null)
const pendingFile = ref<File | null>(null)
const importing = ref(false)
const importResult = ref<ImportResult | null>(null)
const detail = ref<(Row & { 示数明细?: Row[] }) | null>(null)

function queryString() {
  const params = new URLSearchParams()
  if (month.value) params.set('month', month.value)
  if (keyword.value.trim()) params.set('keyword', keyword.value.trim())
  if (status.value) params.set('status', status.value)
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

function resetFilters() {
  month.value = ''
  keyword.value = ''
  status.value = ''
  void reload()
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}${queryString()}`)
    if (!response.ok) {
      throw new Error((await response.json()).detail || '关口表计列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length

    const statsResponse = await request(`/api/meter/stats${month.value ? `?month=${month.value}` : ''}`)
    if (statsResponse.ok) {
      const s = await statsResponse.json()
      stats.value = [
        { label: `正向有功电量合计（${s.月份}）`, value: s.正向有功电量合计 || '0' },
        { label: `反向有功电量合计（${s.月份}）`, value: s.反向有功电量合计 || '0' },
        { label: '异常表计数', value: s.异常表计数 },
      ]
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '关口计量列表读取失败'
  }
}

function triggerImport() {
  errorMessage.value = ''
  fileInput.value?.click()
}

async function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement
  pendingFile.value = input.files?.[0] ?? null
  if (pendingFile.value) {
    await submitImport(0)
  }
  input.value = ''
}

async function submitImport(retry: number): Promise<ImportResult | null> {
  if (!pendingFile.value) return null
  importing.value = true
  errorMessage.value = ''
  try {
    const content = await pendingFile.value.text()
    const suffix = retry ? `?retry=${retry}` : ''
    const response = await request(`${ENDPOINT}/imports${suffix}`, {
      method: 'POST',
      body: JSON.stringify({
        filename: pendingFile.value.name,
        content,
        month: month.value || null,
      }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail || '抄表文件导入失败')
    }
    importResult.value = payload as ImportResult
    await reload()
    return payload as ImportResult
  } catch (error) {
    // 整体失败（网络/服务端错误）：允许自动重试一次
    if (retry === 0) {
      return submitImport(1)
    }
    importResult.value = {
      ok: false, total: 0, imported_count: 0, rejected_count: 0, imported: [], rejected: [],
      fatal: error instanceof Error ? error.message : '抄表文件导入失败',
      retried: true,
    }
    return importResult.value
  } finally {
    importing.value = false
    if (retry >= 1) pendingFile.value = null
  }
}

function retryImport() {
  void submitImport(1)
}

function downloadTemplate() {
  window.open(`${ENDPOINT}/import-template`, '_blank')
}

function exportRows(format: 'csv' | 'json') {
  // 导出带当前月份条件：不传月份时后端按各表最后一次导入值出账，与列表完全同源
  const params = new URLSearchParams({ format })
  if (month.value) params.set('month', month.value)
  window.open(`${ENDPOINT}/export?${params.toString()}`, '_blank')
}

async function showDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('示数明细读取失败')
    }
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '示数明细读取失败'
  }
}

async function refetch(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/refetch`, {
      method: 'POST',
      body: JSON.stringify({ values: { month: month.value || null } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || payload.detail || '重新取数失败')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重新取数失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '关口计量动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '关口计量操作失败'
  }
}

onMounted(reload)
</script>
