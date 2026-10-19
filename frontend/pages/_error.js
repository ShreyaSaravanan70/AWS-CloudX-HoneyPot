export default function Error({ statusCode }) {
  return (
    <main className="error-page">
      <h1>Unable to display this page</h1>
      <p>Error {statusCode}. Please return to the dashboard and try again.</p>
      <a className="button button-coral" href="/">Return to dashboard</a>
    </main>
  )
}

Error.getInitialProps = ({ err, req }) => ({
  statusCode: err ? err.statusCode : 500,
})
