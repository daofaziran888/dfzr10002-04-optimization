import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/datasource'
const columns = [
  "连接编号",
  "连接名称",
  "数据库类型",
  "连接地址",
  "认证凭据",
  "默认库名",
  "连接池大小",
  "是否启用"
]
const actions = [
  "提交校验",
  "修复异常",
  "启用配置",
  "停用配置"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "分析数据源总数",
    "value": 0
  },
  {
    "label": "待校验数据源",
    "value": 0
  },
  {
    "label": "已启用数据源",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type PoolStatus = {
  limit: number
  safety_reserve: number
  effective_limit: number
  leased: number
  available: number
  cooldown_seconds: number
  cooldowns: { entry_id: number; remaining_seconds: number }[]
  dataset_blocked?: number
  dataset_refreshable?: number
  "连接池上限"?: number
  "当前租用"?: number
  "剩余额度"?: number
}

type ActionResultPayload = {
  ok: boolean
  message: string
  reason?: string | null
  retryable?: boolean
  pool?: PoolStatus | null
}

type DependentDatasets = {
  items: Row[]
  total: number
  empty_message: string
  refresh_available: boolean
  refresh_hint: string
}

export default function DatasourceView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [pool, setPool] = useState<PoolStatus | null>(null)
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [expanded, setExpanded] = useState<number | null>(null)
  const [dependents, setDependents] = useState<Record<number, DependentDatasets>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('数据源连接列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setPool(payload.meta?.pool ?? null)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据源列表读取失败')
    }
  }, [filters])

  const loadDependents = useCallback(async (entryId: number) => {
    try {
      const response = await request(`${ENDPOINT}/${entryId}`)
      if (!response.ok) {
        throw new Error('依赖数据集读取失败')
      }
      const payload = await response.json()
      setDependents((prev) => ({ ...prev, [entryId]: payload['依赖数据集'] }))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '依赖数据集读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = (await response.json().catch(() => null)) as ActionResultPayload | null
      if (!response.ok) {
        throw new Error(payload?.message || '分析数据源动作未生效，请稍后重试')
      }
      if (payload && !payload.ok) {
        setErrorMessage(payload.message)
      } else {
        setNoticeMessage(payload?.message ?? `${action}已完成`)
      }
      if (expanded !== null) {
        void loadDependents(expanded)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据源操作失败')
    }
  }

  async function toggleDependents(row: Row) {
    const id = Number(row.id)
    if (expanded === id) {
      setExpanded(null)
      return
    }
    setExpanded(id)
    await loadDependents(id)
  }

  const cooldownById = new Map((pool?.cooldowns ?? []).map((item) => [item.entry_id, item.remaining_seconds]))
  const currentLeased = pool?.leased ?? Number(pool?.['当前租用'] ?? 0)
  const available = pool?.available ?? Number(pool?.['剩余额度'] ?? 0)
  const limit = pool?.limit ?? Number(pool?.['连接池上限'] ?? 0)

  return (
    <section className="page" data-module="datasource">
      <header className="page-head">
        <div>
          <h2>分析数据源管理</h2>
          <p className="page-desc">启用准入按连接池余量、当前租用数与冷却窗口判断，达到上限时在启用阶段说明原因。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('数据源连接登记入口尚未接入审批流')}>
            登记数据源连接
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析数据源清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item, index) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">
              {index === 0 ? total : index === 1 ? rows.filter((row) => row.status === '待校验').length : rows.filter((row) => row.status === '已启用').length}
            </strong>
          </article>
        ))}
        <article className="stat-card">
          <span className="stat-label">连接池剩余额度</span>
          <strong className="stat-value">{available} / {limit}</strong>
          <span className="stat-label">当前租用 {currentLeased}，安全预留 {pool?.safety_reserve ?? 0}</span>
        </article>
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const entryId = Number(row.id)
            const cooldown = cooldownById.get(entryId)
            const detail = dependents[entryId]
            return (
              <Fragment key={String(row.id)}>
                <tr>
                  {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                  <td className="row-actions">
                    {actions.map((action) => {
                      const cooling = action === '启用配置' && cooldown && cooldown > 0
                      return (
                        <button
                          key={action}
                          className="link"
                          type="button"
                          disabled={Boolean(cooling)}
                          title={cooling ? `冷却中，剩余 ${Math.ceil(Number(cooldown))} 秒` : undefined}
                          onClick={() => void runAction(action, row)}
                        >
                          {action}
                          {cooling ? `（冷却 ${Math.ceil(Number(cooldown))}s）` : ''}
                        </button>
                      )
                    })}
                    <button className="link" type="button" onClick={() => void toggleDependents(row)}>
                      {expanded === entryId ? '收起依赖数据集' : '依赖数据集'}
                    </button>
                  </td>
                </tr>
                {expanded === entryId && (
                  <tr key={`${row.id}-deps`}>
                    <td colSpan={columns.length + 1}>
                      {detail ? (
                        detail.total === 0 ? (
                          <span className="empty-state">{detail.empty_message}</span>
                        ) : (
                          <div>
                            <p className="page-desc">{detail.refresh_hint}</p>
                            <table className="data-table">
                              <thead>
                                <tr><th>数据集</th><th>状态</th><th>刷新可用</th><th>刷新边界</th></tr>
                              </thead>
                              <tbody>
                                {detail.items.map((item) => (
                                  <tr key={String(item.id)}>
                                    <td>{item['数据集编号']}</td>
                                    <td>{item.status}</td>
                                    <td>{item['刷新可用'] ? '可刷新' : '已拦下'}</td>
                                    <td>{item['刷新边界'] ?? '—'}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )
                      ) : (
                        <span className="empty-state">正在读取依赖数据集…</span>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无分析数据源数据，可先登记数据源连接</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条分析数据源记录</span>
        {noticeMessage && <span className="ok-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
