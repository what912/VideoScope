import { useId, type ChangeEvent, type DragEvent } from "react";

import type { Dictionary } from "../../i18n/types";

interface UploadDropzoneProps {
  copy: Dictionary["upload"];
  dragging: boolean;
  file: File | null;
  onDragStateChange(dragging: boolean): void;
  onFile(file: File): void;
}

export function UploadDropzone({
  copy,
  dragging,
  file,
  onDragStateChange,
  onFile,
}: UploadDropzoneProps) {
  const inputId = useId();

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0];
    if (selected) onFile(selected);
    event.target.value = "";
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    onDragStateChange(false);
    const selected = event.dataTransfer.files[0];
    if (selected) onFile(selected);
  }

  return (
    <div
      className="upload-dropzone"
      data-dragging={String(dragging)}
      data-testid="upload-dropzone"
      onDragEnter={(event) => {
        event.preventDefault();
        onDragStateChange(true);
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node)) {
          onDragStateChange(false);
        }
      }}
      onDragOver={(event) => event.preventDefault()}
      onDrop={drop}
    >
      <div aria-hidden="true" className="upload-dropzone__scope">
        <span />
      </div>
      <p className="upload-dropzone__title">
        {dragging ? copy.dropActive : copy.dropIdle}
      </p>
      {file ? (
        <p className="upload-dropzone__file">
          <span>{copy.selectedFile}</span>
          <strong>{file.name}</strong>
        </p>
      ) : null}
      <label className="button button--primary" htmlFor={inputId}>
        {copy.browse}
      </label>
      <input
        accept="video/mp4,video/webm,video/quicktime,video/x-matroska,.mp4,.webm,.mov,.mkv"
        aria-label={copy.chooseFile}
        className="visually-hidden"
        id={inputId}
        onChange={chooseFile}
        type="file"
      />
      <small>{copy.supported}</small>
    </div>
  );
}
