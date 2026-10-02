// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Executes one controller-owned asynchronous video task.
package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var err error
	if len(os.Args) == 2 && os.Args[1] == "--cleanup" {
		err = cleanup()
	} else if len(os.Args) != 1 {
		err = fmt.Errorf("usage: video-worker [--cleanup]")
	} else {
		err = run(ctx)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// run owns the request, mounted files, and output publication for one worker Job.
func run(ctx context.Context) error {
	var request api.VideoRequest
	if value := os.Getenv("FORETOKEN_VIDEO_REQUEST_JSON"); value == "" {
		return fmt.Errorf("FORETOKEN_VIDEO_REQUEST_JSON is required")
	} else if err := json.Unmarshal([]byte(value), &request); err != nil {
		return fmt.Errorf("decode video request: %w", err)
	}
	endpoint := os.Getenv("FORETOKEN_VIDEO_ENDPOINT")
	model := os.Getenv("FORETOKEN_VIDEO_MODEL")
	outputPath := os.Getenv("FORETOKEN_VIDEO_OUTPUT_PATH")
	outputMount := os.Getenv("FORETOKEN_VIDEO_OUTPUT_MOUNT")
	if endpoint == "" || model == "" || outputPath == "" || outputMount == "" {
		return fmt.Errorf("FORETOKEN_VIDEO_ENDPOINT, FORETOKEN_VIDEO_MODEL, FORETOKEN_VIDEO_OUTPUT_PATH and FORETOKEN_VIDEO_OUTPUT_MOUNT are required")
	}
	if !filepath.IsLocal(outputPath) {
		return fmt.Errorf("FORETOKEN_VIDEO_OUTPUT_PATH must be local to the output mount")
	}
	for _, input := range request.InputFiles {
		if !filepath.IsLocal(input.Path) {
			return fmt.Errorf("input path %q must be local to the output mount", input.Path)
		}
	}
	if seconds := os.Getenv("FORETOKEN_VIDEO_TIMEOUT_SECONDS"); seconds != "" {
		timeout, err := time.ParseDuration(seconds + "s")
		if err != nil || timeout <= 0 {
			return fmt.Errorf("FORETOKEN_VIDEO_TIMEOUT_SECONDS must be a positive number of seconds")
		}
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}

	root, err := os.OpenRoot(outputMount)
	if err != nil {
		return fmt.Errorf("open output mount: %w", err)
	}
	defer root.Close()

	// Kubernetes may replace a lost Job Pod before its predecessor is known to have stopped.
	// Claim this task once on durable storage; an uncertain attempt is never retried automatically.
	parentPath := filepath.Dir(outputPath)
	if err := root.MkdirAll(parentPath, 0o750); err != nil {
		return err
	}
	claim, err := root.OpenFile(filepath.Join(parentPath, "execution.started"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o640)
	if err != nil {
		return fmt.Errorf("claim video execution: %w", err)
	}
	if err := errors.Join(claim.Sync(), claim.Close()); err != nil {
		return err
	}
	directory, err := root.Open(parentPath)
	if err != nil {
		return err
	}
	if err := errors.Join(directory.Sync(), directory.Close()); err != nil {
		return err
	}

	reader, pipeWriter := io.Pipe()
	multipartWriter := multipart.NewWriter(pipeWriter)
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint+"/v1/videos/sync", reader)
	if err != nil {
		reader.Close()
		pipeWriter.Close()
		return err
	}
	httpRequest.Header.Set("Content-Type", multipartWriter.FormDataContentType())
	writeResult := make(chan error, 1)
	go func() {
		writeErr := writeMultipart(multipartWriter, root, request, model)
		writeErr = errors.Join(writeErr, multipartWriter.Close())
		pipeWriter.CloseWithError(writeErr)
		writeResult <- writeErr
	}()
	// A cancelled request must unblock a writer waiting on io.Pipe even if the backend stops reading.
	stopPipe := context.AfterFunc(ctx, func() { reader.CloseWithError(ctx.Err()) })
	defer stopPipe()
	defer reader.Close()

	response, err := (&http.Client{}).Do(httpRequest)
	if err != nil {
		reader.CloseWithError(err)
		if writeErr := <-writeResult; writeErr != nil && ctx.Err() == nil {
			return fmt.Errorf("write video request: %w", writeErr)
		}
		return fmt.Errorf("request video: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		reader.CloseWithError(fmt.Errorf("video backend returned %s", response.Status))
		<-writeResult
		return fmt.Errorf("video backend returned %s", response.Status)
	}
	// An early success response must not keep the pipe writer blocked indefinitely.
	select {
	case writeErr := <-writeResult:
		if writeErr != nil {
			return fmt.Errorf("write video request: %w", writeErr)
		}
	default:
		reader.CloseWithError(fmt.Errorf("backend replied before consuming the request"))
		if writeErr := <-writeResult; writeErr != nil {
			return fmt.Errorf("write video request: %w", writeErr)
		}
	}
	return publishResult(ctx, root, outputPath, response.Body)
}

// cleanup removes only the controller-owned task directory from the shared output mount.
func cleanup() error {
	id := os.Getenv("FORETOKEN_VIDEO_TASK_ID")
	mount := os.Getenv("FORETOKEN_VIDEO_OUTPUT_MOUNT")
	if id == "." || !filepath.IsLocal(id) || filepath.Base(id) != id || mount == "" {
		return fmt.Errorf("FORETOKEN_VIDEO_TASK_ID must be one local path component and FORETOKEN_VIDEO_OUTPUT_MOUNT is required")
	}
	root, err := os.OpenRoot(mount)
	if err != nil {
		return err
	}
	defer root.Close()
	return root.RemoveAll(filepath.Join("tasks", id))
}

// publishResult syncs a private file before atomically exposing the completed artifact.
func publishResult(ctx context.Context, root *os.Root, outputPath string, body io.Reader) error {
	parentPath := filepath.Dir(outputPath)
	if err := root.MkdirAll(parentPath, 0o750); err != nil {
		return err
	}
	parent, err := root.OpenRoot(parentPath)
	if err != nil {
		return err
	}
	defer parent.Close()

	var suffix [16]byte
	if _, err := rand.Read(suffix[:]); err != nil {
		return err
	}
	temporary := fmt.Sprintf(".video-%x.tmp", suffix)
	output, err := parent.OpenFile(temporary, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o640)
	if err != nil {
		return err
	}
	defer parent.Remove(temporary)
	written, copyErr := io.Copy(output, body)
	if copyErr == nil && written == 0 {
		copyErr = fmt.Errorf("video backend returned an empty result")
	}
	var syncErr error
	if copyErr == nil {
		syncErr = output.Sync()
	}
	closeErr := output.Close()
	if err := errors.Join(copyErr, syncErr, closeErr, ctx.Err()); err != nil {
		return fmt.Errorf("write video result: %w", err)
	}
	if err := parent.Rename(temporary, filepath.Base(outputPath)); err != nil {
		return fmt.Errorf("publish video result: %w", err)
	}
	// The directory entry must also reach storage before a successful Job publishes its artifact.
	directory, err := parent.Open(".")
	if err != nil {
		return err
	}
	return errors.Join(directory.Sync(), directory.Close())
}

// writeMultipart maps the backend-neutral VideoRequest onto the synchronous video endpoint.
func writeMultipart(writer *multipart.Writer, root *os.Root, request api.VideoRequest, model string) error {
	extraParams := map[string]any{"task": request.Task}
	if request.AudioFlowShift != nil {
		extraParams["audio_flow_shift"] = *request.AudioFlowShift
	}
	if len(request.FrameIndices) > 0 {
		extraParams["frame_indices"] = request.FrameIndices
	}
	extraJSON, err := json.Marshal(extraParams)
	if err != nil {
		return err
	}
	fields := map[string]string{
		"model":               model,
		"prompt":              request.Prompt,
		"width":               strconv.FormatInt(int64(request.Width), 10),
		"height":              strconv.FormatInt(int64(request.Height), 10),
		"num_frames":          strconv.FormatInt(int64(request.NumFrames), 10),
		"fps":                 strconv.FormatInt(int64(request.FPS), 10),
		"num_inference_steps": strconv.FormatInt(int64(request.NumInferenceSteps), 10),
		"extra_params":        string(extraJSON),
	}
	if request.AspectRatio != "" {
		fields["aspect_ratio"] = request.AspectRatio
	}
	if request.FlowShift != nil {
		fields["flow_shift"] = strconv.FormatFloat(*request.FlowShift, 'f', -1, 64)
	}
	if request.Seed != nil {
		fields["seed"] = strconv.FormatInt(*request.Seed, 10)
	}
	for name, value := range fields {
		if err := writer.WriteField(name, value); err != nil {
			return err
		}
	}
	for _, input := range request.InputFiles {
		file, err := root.Open(input.Path)
		if err != nil {
			return fmt.Errorf("open %s: %w", input.Path, err)
		}
		header := make(textproto.MIMEHeader)
		header.Set("Content-Disposition", fmt.Sprintf(`form-data; name=%q; filename=%q`, input.Field, filepath.Base(input.Path)))
		if input.ContentType != "" {
			header.Set("Content-Type", input.ContentType)
		}
		part, err := writer.CreatePart(header)
		if err == nil {
			_, err = io.Copy(part, file)
		}
		closeErr := file.Close()
		if err := errors.Join(err, closeErr); err != nil {
			return err
		}
	}
	return nil
}
