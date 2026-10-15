export function businessDate(date = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date)
  const get = (kind: string) => parts.find(part => part.type === kind)?.value || ''
  return `${get('year')}-${get('month')}-${get('day')}`
}
