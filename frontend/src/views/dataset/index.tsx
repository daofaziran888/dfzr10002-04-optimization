import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dataset'
const columns = [
  "数据集编号",
  "数据集名称",
  "来源表名",
  "字段列表",
  "行数上限",
  "刷新频率",
  "最近刷新",
  "是否启用",
  "刷新边界"
]
const actions = [
  "提交校验",
  "启用配置",
  "停用配置",
  "刷新数据集"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "分析数据集总数",
    "value": 0
  },
  {
    "label": "待校验数据集",
    "value": 0
  },
  {
    "label": "已启用数据集",
    "value": 0
  }
]

type Row = Record<string, string | number | boolean | null>

type ActionResultPayload = {
  ok: boolean
  message: string
  reason?: string | null
  retryable?: boolean
}

export default function DatasetView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [lastFailure, setLastFailure] = useState<{ id: number; action: string } | null>(null)
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('数据集列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据集列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    setLastFailure(null)
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = (await response.json().catch(() => null)) as ActionResultPayload | null
      if (!response.ok) {
        throw new Error(payload?.message || '分析数据集动作未生效，请稍后重试')
      }
      if (payload && !payload.ok) {
        setErrorMessage(payload.message)
        setLastFailure(payload.retryable ? { id: Number(row.id), action } : null)
      } else {
        setNoticeMessage(payload?.message ?? `${action}已完成`)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据集操作失败')
    }
  }

  async function retryLastFailure() {
    if (!lastFailure) {
      return
    }
    const row = rows.find((item) => Number(item.id) === lastFailure.id)
    if (row) {
      await runAction(lastFailure.action, row)
    }
  }

  return (
    <section className="page" data-module="dataset">
      <header className="page-head">
        <div>
          <h2>分析数据集管理</h2>
          <p className="page-desc">维护分析可用的数据集定义、字段范围与刷新策略；依赖的数据源停用后刷新会被同步拦下。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('数据集登记入口尚未接入审批流')}>
            登记数据集
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析数据集清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item, index) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">
              {index === 0
                ? total
                : index === 1
                  ? rows.filter((row) => row.status === '待校验').length
                  : rows.filter((row) => row.status === '已启用').length}
            </strong>
          </article>
        ))}
        <article className="stat-card">
          <span className="stat-label">可刷新数据集</span>
          <strong className="stat-value">{rows.filter((row) => row['刷新可用']).length} / {rows.length}</strong>
          <span className="stat-label">{rows.filter((row) => !row['刷新可用'] && row.status !== '已停用').length} 条被连接状态拦下</span>
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
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column}>
                  {column === '刷新边界'
                    ? (row[column] ?? '—')
                    : column === '是否启用' && row['刷新可用'] === false && row.status !== '已停用'
                      ? `${row[column] ?? '—'}（刷新已拦下）`
                      : (row[column] ?? '—')}
                </td>
              ))}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无分析数据集数据，可先登记数据集</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条分析数据集记录</span>
        {noticeMessage && <span className="ok-text">{noticeMessage}</span>}
        {errorMessage && (
          <span className="error-text">
            {errorMessage}
            {lastFailure && (
              <button className="link" type="button" onClick={() => void retryLastFailure()}>
                重试
              </button>
            )}
          </span>
        )}
      </footer>
    </section>
  )
}
