import LogoIcon from '@/components/Brand/LogoIcon'
import { schemas } from '@polar-sh/client'

const generatePostOGFallbackPath = (slug: string, maxInt: number) => {
  let sum = 0
  for (let i = 0; i < slug.length; i++) {
    sum += slug.charCodeAt(i)
  }
  return `${sum % maxInt}.jpg`
}

const imageBaseURL = 'https://polar.sh/assets/posts/og'

const OpenGraphImageCreator = ({
  organization,
}: {
  organization: schemas['Organization']
}) => {
  return (
    <div
      style={{
        position: 'relative',
        height: 630,
        width: 1200,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'space-between',
        background: 'white',
        backgroundImage: `url(${imageBaseURL}/${generatePostOGFallbackPath(
          organization.slug,
          7,
        )})`,
        backgroundSize: 'cover',
        backgroundPosition: 'center',
        color: 'white',
        padding: '64px 92px',
        gap: '24px',
        whiteSpace: 'pre-wrap',
        fontWeight: 600,
        fontSize: '48px',
        textAlign: 'center',
        /** @ts-ignore */
        textWrap: 'balance',
      }}
    >
      {organization.avatar_url && (
        <img
          src={organization.avatar_url}
          height={160}
          width={160}
          style={{
            height: 160,
            width: 160,
            borderRadius: 160,
            flexShrink: 0,
          }}
        />
      )}
      <div
        style={{
          fontWeight: 500,
          fontSize: '64px',
          whiteSpace: 'nowrap',
        }}
      >
        {organization.name}
      </div>
      <LogoIcon size={72} />
    </div>
  )
}

export default OpenGraphImageCreator
