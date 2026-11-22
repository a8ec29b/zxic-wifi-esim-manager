package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/damonto/euicc-go/bertlv"
	"github.com/damonto/euicc-go/bertlv/primitive"
	"github.com/damonto/euicc-go/lpa"
	sgp22 "github.com/damonto/euicc-go/v2"
)

type request struct {
	Device       string `json:"device"`
	IMEI         string `json:"imei"`
	Activation   string `json:"activation"`
	Confirmation string `json:"confirmation"`
	Action       string `json:"action"`
	ICCID        string `json:"iccid"`
	Nickname     string `json:"nickname"`
	Sequence     int64  `json:"sequence"`
}
type response struct {
	Channel  byte   `json:"channel"`
	Response string `json:"response"`
	Error    string `json:"error"`
}
type cardChannel struct {
	device  string
	cmd     *exec.Cmd
	input   io.WriteCloser
	output  *bufio.Reader
	channel byte
}

func (c *cardChannel) receive() (response, error) {
	line, err := c.output.ReadBytes('\n')
	if err != nil {
		return response{}, fmt.Errorf("USB 通道中断: %w", err)
	}
	var r response
	if err = json.Unmarshal(line, &r); err != nil {
		return r, err
	}
	if r.Error != "" {
		return r, fmt.Errorf("%s", r.Error)
	}
	return r, nil
}
func (c *cardChannel) Connect() error {
	c.cmd = exec.Command("/usr/bin/python3", "-u", "esim_transport.py", c.device)
	c.cmd.Stderr = os.Stderr
	var err error
	c.input, err = c.cmd.StdinPipe()
	if err != nil {
		return err
	}
	output, err := c.cmd.StdoutPipe()
	if err != nil {
		return err
	}
	c.output = bufio.NewReader(output)
	if err = c.cmd.Start(); err != nil {
		return err
	}
	r, err := c.receive()
	c.channel = r.Channel
	return err
}
func (c *cardChannel) OpenLogicalChannel(aid []byte) (byte, error) {
	if c.channel == 0 {
		return 0, fmt.Errorf("卡通道未打开")
	}
	return c.channel, nil
}
func (c *cardChannel) Transmit(command []byte) ([]byte, error) {
	if len(command) > 1 && command[1] == 0xc0 {
		command[0] &= 0x7f
	}
	if err := json.NewEncoder(c.input).Encode(map[string]string{"apdu": hex.EncodeToString(command)}); err != nil {
		return nil, err
	}
	r, err := c.receive()
	if err != nil {
		return nil, err
	}
	return hex.DecodeString(r.Response)
}
func (c *cardChannel) CloseLogicalChannel(channel byte) error { return nil }
func (c *cardChannel) Disconnect() error {
	if c.input != nil {
		c.input.Close()
	}
	if c.cmd != nil && c.cmd.Process != nil {
		return c.cmd.Wait()
	}
	return nil
}
func run(req request) (any, error) {
	timeout := 60 * time.Second
	if req.Action == "notify" {
		timeout = 15 * time.Second
	}
	client, err := lpa.New(&lpa.Options{Channel: &cardChannel{device: req.Device}, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Timeout: timeout})
	if err != nil {
		return nil, err
	}
	defer client.Close()
	if req.Action == "info" {
		_, err = client.EUICCInfo2()
		if err != nil {
			return nil, err
		}
		return map[string]bool{"ok": true}, nil
	}
	if req.Action == "inventory" {
		return inventory(client)
	}
	if req.Action == "enable" || req.Action == "disable" || req.Action == "delete-profile" || req.Action == "nickname" {
		iccid, err := sgp22.NewICCID(req.ICCID)
		if err != nil {
			return nil, err
		}
		switch req.Action {
		case "enable":
			err = client.EnableProfile(iccid, true)
		case "disable":
			err = client.DisableProfile(iccid, true)
		case "delete-profile":
			err = client.DeleteProfile(iccid)
		case "nickname":
			err = client.SetNickname(iccid, req.Nickname)
		}
		if err != nil {
			return nil, err
		}
		return map[string]string{"result": "success"}, nil
	}
	if req.Action == "list-notifications" {
		notifications, err := client.ListNotification()
		if err != nil {
			return nil, err
		}
		type metadata struct {
			Sequence int64  `json:"sequence"`
			ICCID    string `json:"iccid"`
			Event    byte   `json:"event"`
		}
		items := []metadata{}
		for _, n := range notifications {
			items = append(items, metadata{Sequence: int64(n.SequenceNumber), ICCID: n.ICCID.String(), Event: byte(n.ProfileManagementOperation)})
		}
		return items, nil
	}
	if req.Action == "notify" {
		if err := sendNotification(client, sgp22.SequenceNumber(req.Sequence)); err != nil {
			return nil, err
		}
		return map[string]bool{"ok": true}, nil
	}
	var ac lpa.ActivationCode
	if err = ac.UnmarshalText([]byte(req.Activation)); err != nil {
		return nil, err
	}
	ac.IMEI = req.IMEI
	ac.ConfirmationCode = req.Confirmation
	existing, err := client.ListNotification()
	if err != nil {
		return nil, fmt.Errorf("无法读取现有运营商通知: %w", err)
	}
	var before sgp22.SequenceNumber
	for _, n := range existing {
		if n.SequenceNumber > before {
			before = n.SequenceNumber
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Minute)
	defer cancel()
	result, err := client.DownloadProfile(ctx, &ac, &lpa.DownloadOptions{OnConfirm: func(_ *sgp22.ProfileInfo) bool { return true }})
	if err != nil {
		return nil, err
	}
	if result == nil {
		return nil, fmt.Errorf("下载未完成")
	}
	resultData := map[string]any{"ok": true}
	sequences := map[sgp22.SequenceNumber]bool{}
	if result.Notification != nil && result.Notification.SequenceNumber > 0 {
		sequences[result.Notification.SequenceNumber] = true
	}
	current, listErr := client.ListNotification(sgp22.NotificationEventInstall)
	if listErr != nil {
		resultData["warning"] = "配置已安装，但无法核实其他运营商通知: " + listErr.Error()
	} else if result.Notification != nil {
		for _, n := range current {
			if n.SequenceNumber > before && len(result.Notification.ICCID) > 0 && bytes.Equal(n.ICCID, result.Notification.ICCID) {
				sequences[n.SequenceNumber] = true
			}
		}
	} else {
		resultData["warning"] = "配置已安装，但无法确定对应的运营商通知"
	}
	var failures []int64
	for sequence := range sequences {
		if err := sendNotification(client, sequence); err != nil {
			failures = append(failures, int64(sequence))
		}
	}
	if len(failures) > 0 {
		resultData["notification_failures"] = failures
		warning, _ := resultData["warning"].(string)
		resultData["warning"] = strings.TrimSpace(warning + " 部分运营商通知未送达，后台将重试")
	}
	return resultData, nil
}
func inventory(client *lpa.Client) (any, error) {
	eid, err := client.EID()
	if err != nil {
		return nil, err
	}
	profiles, err := client.ListProfile(nil, nil)
	if err != nil {
		return nil, err
	}
	info, err := client.EUICCInfo2()
	if err != nil {
		return nil, err
	}
	resource := info.First(bertlv.ContextSpecific.Primitive(4))
	if resource == nil {
		return nil, fmt.Errorf("卡未报告剩余空间")
	}
	reader := bytes.NewReader(resource.Value)
	var free int32
	found := false
	for reader.Len() > 0 {
		var field bertlv.TLV
		if _, err = field.ReadFrom(reader); err != nil {
			return nil, err
		}
		if field.Tag.Equal(bertlv.ContextSpecific.Primitive(2)) {
			if err = field.UnmarshalValue(primitive.UnmarshalInt(&free)); err != nil {
				return nil, err
			}
			found = true
		}
	}
	if !found {
		return nil, fmt.Errorf("卡未报告剩余空间")
	}
	type profile struct {
		ICCID    string `json:"iccid"`
		State    int8   `json:"state"`
		Nickname string `json:"nickname"`
		Provider string `json:"provider"`
		Name     string `json:"name"`
	}
	result := make([]profile, 0, len(profiles))
	for _, p := range profiles {
		result = append(result, profile{ICCID: p.ICCID.String(), State: int8(p.ProfileState), Nickname: p.ProfileNickname, Provider: p.ServiceProviderName, Name: p.ProfileName})
	}
	return map[string]any{"eid": strings.ToUpper(hex.EncodeToString(eid)), "profiles": result, "free_bytes": free}, nil
}
func sendNotification(client *lpa.Client, sequence sgp22.SequenceNumber) error {
	pending, err := client.RetrieveNotificationList(sequence)
	if err != nil {
		return err
	}
	if len(pending) == 0 {
		return fmt.Errorf("卡中没有找到通知 %d", sequence)
	}
	for _, n := range pending {
		if err = client.HandleNotification(n); err != nil {
			return err
		}
	}
	return nil
}
func main() {
	var req request
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		json.NewEncoder(os.Stdout).Encode(map[string]string{"error": err.Error()})
		return
	}
	result, err := run(req)
	if err != nil {
		json.NewEncoder(os.Stdout).Encode(map[string]string{"error": err.Error()})
		return
	}
	json.NewEncoder(os.Stdout).Encode(result)
}
