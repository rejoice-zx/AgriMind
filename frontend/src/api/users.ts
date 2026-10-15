import request from '@/utils/request'
import type { User, PaginatedResponse } from '@/types'

export interface UserListParams {
  page?: number
  page_size?: number
  role?: string
  is_active?: boolean
  username?: string
}

export interface CreateUserRequest {
  username: string
  password: string
  display_name: string
  phone: string
  email?: string
  role: string
}

export interface UpdateUserRequest {
  phone?: string
  display_name?: string
  email?: string
  role?: string
  is_active?: boolean
}

export interface PendingRegistration extends User {
  created_at: string
}

export function getUsers(params: UserListParams = {}) {
  return request.get<PaginatedResponse<User>>('/users', { params })
}

export function createUser(data: CreateUserRequest) {
  return request.post<User>('/users', data)
}

export function updateUser(id: number, data: UpdateUserRequest) {
  return request.put<User>(`/users/${id}`, data)
}

export function deleteUser(id: number) {
  return request.delete(`/users/${id}`)
}

export function getPendingRegistrations() {
  return request.get<PendingRegistration[]>('/users/pending')
}

export function getRegistrations(params: { page: number; page_size: number; registration_status?: 'pending' | 'approved' | 'rejected' }) {
  return request.get<PaginatedResponse<PendingRegistration>>('/users/registrations', { params })
}

export function approveRegistration(userId: number) {
  return request.post(`/users/${userId}/approve`)
}

export function rejectRegistration(userId: number) {
  return request.post(`/users/${userId}/reject`)
}

export function resetPassword(userId: number, newPassword: string) {
  return request.post(`/users/${userId}/reset-password`, { password: newPassword })
}
