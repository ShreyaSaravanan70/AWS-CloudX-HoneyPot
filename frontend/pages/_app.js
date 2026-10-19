
import "../styles/globals.css";
import Head from "next/head";

export default function App({ Component, pageProps }) {
  return (
    <>
      <Head>
        <title>HaaS Security Dashboard</title>
        <meta
          name="viewport"
          content="width=device-width, initial-scale=1"
        />
        <meta
          name="description"
          content="Monitor Cowrie honeypot events from the configured LocalStack or AWS pipeline."
        />
      </Head>
      <Component {...pageProps} />
    </>
  );
}
