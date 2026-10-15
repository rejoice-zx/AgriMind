import MarkdownIt from 'markdown-it'

// User/model HTML stays inert. Uploaded images use AuthenticatedImage separately;
// Markdown must not trigger unauthenticated or third-party image requests.
const markdown = new MarkdownIt({ html: false, breaks: true, linkify: false })
markdown.disable('image')
const defaultValidateLink = markdown.validateLink.bind(markdown)
markdown.validateLink = href => defaultValidateLink(href)
  && !/^[\s]*\/\//.test(href)
  && (!/^[a-z][a-z\d+.-]*:/i.test(href) || /^(https?:|mailto:)/i.test(href))

const defaultLinkOpen = markdown.renderer.rules.link_open
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index]!.attrSet('target', '_blank')
  tokens[index]!.attrSet('rel', 'noopener noreferrer nofollow')
  return defaultLinkOpen
    ? defaultLinkOpen(tokens, index, options, env, renderer)
    : renderer.renderToken(tokens, index, options)
}

export function renderSafeMarkdown(content: string): string {
  return content ? markdown.render(content) : ''
}
