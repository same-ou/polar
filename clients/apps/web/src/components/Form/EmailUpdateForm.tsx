'use client'

import { useSendEmailUpdate } from '@/hooks/emailUpdate'
import { setValidationErrors } from '@/utils/api/errors'
import { FormControl } from '@mui/material'
import Button from '@polar-sh/ui/components/atoms/Button'
import Input from '@polar-sh/ui/components/atoms/Input'
import { Form, FormField, FormItem } from '@polar-sh/ui/components/ui/form'
import { useState } from 'react'
import { SubmitHandler, useForm } from 'react-hook-form'

interface EmailUpdateformProps {
  returnTo?: string
  onEmailUpdateRequest?: () => void
  onEmailUpdateExists?: () => void
  onEmailUpdateForm?: () => void
  setErr?: (value: string | null) => void
}

const EmailUpdateForm: React.FC<EmailUpdateformProps> = ({
  returnTo,
  onEmailUpdateRequest,
  onEmailUpdateExists,
  onEmailUpdateForm,
}) => {
  const form = useForm<{ email: string }>()
  const { control, handleSubmit, setError } = form
  const [loading, setLoading] = useState(false)
  const sendEmailUpdate = useSendEmailUpdate()

  const onSubmit: SubmitHandler<{ email: string }> = async ({ email }) => {
    setLoading(true)
    const { error } = await sendEmailUpdate(email, returnTo)
    setLoading(false)
    if (error) {
      if (error.detail) {
        setValidationErrors(error.detail, setError)
      }
      onEmailUpdateExists?.()
      setTimeout(() => {
        onEmailUpdateForm?.()
      }, 6000)
      return
    }
    onEmailUpdateRequest?.()
  }

  return (
    <Form {...form}>
      <form className="flex w-full flex-col" onSubmit={handleSubmit(onSubmit)}>
        <FormField
          control={control}
          name="email"
          render={({ field }) => {
            return (
              <FormItem>
                <FormControl className="w-full">
                  <div className="flex w-full flex-row gap-2">
                    <Input
                      type="email"
                      required
                      placeholder="New email"
                      autoComplete="off"
                      data-1p-ignore
                      {...field}
                    />
                    <Button
                      type="submit"
                      size="lg"
                      variant="secondary"
                      loading={loading}
                      disabled={loading}
                    >
                      Update
                    </Button>
                  </div>
                </FormControl>
              </FormItem>
            )
          }}
        />
      </form>
    </Form>
  )
}

export default EmailUpdateForm
