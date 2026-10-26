/** Shows the original document next to the extracted fields. */
export function DocumentPreview({ url, contentType }: { url: string; contentType?: string }) {
  const type = contentType ?? "";
  let body;
  if (type === "application/pdf") {
    body = <iframe className="preview-frame" src={url} title="Original document" />;
  } else if (type.startsWith("image/") && type !== "image/tiff") {
    body = <img className="preview-image" src={url} alt="Original document" />;
  } else {
    body = (
      <p className="preview-fallback">
        This file type can&apos;t be shown in the browser. Open it in a new tab to view it.
      </p>
    );
  }

  return (
    <figure className="preview">
      {body}
      <figcaption>
        <a href={url} target="_blank" rel="noreferrer">Open original in a new tab</a>
        <span className="muted"> · link expires in 5 minutes</span>
      </figcaption>
    </figure>
  );
}
