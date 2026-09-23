export class ApiError extends Error {
  action?: string
  constructor(message: string, action?: string) {
    super(message)
    this.action = action
  }
}
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options?.headers },
    })
  } catch {
    throw new ApiError(
      'Не удалось связаться с приложением.',
      'Проверьте, что локальный сервер WindPulse запущен, и повторите попытку.'
    )
  }
  let body: any
  try {
    body = await response.json()
  } catch {
    throw new ApiError('Сервер вернул неожиданный ответ.', 'Проверьте запуск API и обновите страницу.')
  }
  if (!response.ok) {
    const detail = body.detail
    throw new ApiError(
      typeof detail === 'string' ? detail : (detail?.message ?? 'Запрос не выполнен.'),
      detail?.action
    )
  }
  return body as T
}
