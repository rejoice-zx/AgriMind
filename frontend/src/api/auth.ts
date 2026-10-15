import request from '@/utils/request'
import type { LoginRequest, LoginResponse, CustomerRegisterRequest, RegistrationPendingResponse, User } from '@/types'

export function login(data: LoginRequest) {
  return request.post<LoginResponse>('/auth/login', data)
}

export function registerCustomer(data: CustomerRegisterRequest) {
  return request.post<LoginResponse | RegistrationPendingResponse>('/auth/register-customer', data)
}

export function getCurrentUser() {
  return request.get<User>('/auth/me')
}

export function getGuestToken() {
  return request.post<{ access_token: string; visitor_id: string; expires_in: number }>('/auth/guest')
}
