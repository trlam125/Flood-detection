# Đánh giá model trước khi quyết định fine-tune

Bộ công cụ chạy offline, dùng cùng `predict_mask()` với ứng dụng: cùng chuẩn hóa,
checkpoint, ngưỡng phân đoạn và bước lọc mặt nạ. Công cụ không khởi động FastAPI,
không ghi lịch sử vào database của dashboard và không gửi thông báo.

Chạy từ thư mục gốc project. Các ví dụ dùng PowerShell và môi trường `.venv` hiện có;
không cần cài thêm thư viện.

## 1. Kiểm tra ZIP và bảng màu

```powershell
.\.venv\Scripts\python.exe -m evaluation inspect `
  --archive "media/Flood Amateur Video for Semantic Segmentation Dataset.zip" `
  --output outputs/palette_review
```

Đầu ra:

- `inventory.json`: số cặp, nguồn ảnh, ảnh thiếu nhãn, tên trùng và bảng màu lấy mẫu.
- `previews/`: ảnh gốc cạnh nhãn màu, mặc định 10 mẫu phân bố theo nguồn.

Ảnh và nhãn ghép theo tên file không gồm phần mở rộng. Nếu tên trùng ở nhiều thư
mục, công cụ loại khỏi các cặp và ghi vào `ambiguous_stems`; không đoán cặp ghép.
Lệnh `prepare` chỉ ghi tên file được tạo từ hash, không giải nén trực tiếp đường
dẫn từ ZIP. Phạm vi hỗ trợ là archive có thư mục `image/images` và
`anotation/annotation/annotations/mask/masks`.

### Dataset đang có trong project

ZIP hiện tại chứa 680 cặp từ 5 nguồn và 213 ảnh không có nhãn tương ứng. Không có
tài liệu xác nhận phiên bản dataset hoặc tập train của checkpoint RIWA.

Nguồn tham khảo:

- [Mendeley Data — Flood Amateur Video for Semantic Segmentation Dataset](https://data.mendeley.com/datasets/3kzr8mt8s2/5)
- [Bài báo mô tả dataset](https://pmc.ncbi.nlm.nih.gov/articles/PMC10661843/)

Hai nguồn mô tả xanh đậm/xanh nhạt cho lớp nước và bầu trời không thống nhất.
Đối chiếu trực tiếp ảnh và nhãn cục bộ từ cả 5 nguồn cho thấy lớp nước trong ZIP
này là **RGB `[51, 221, 255]`**, tức **BGR `[255, 221, 51]`** khi đọc bằng OpenCV.
Mã số này được xác định từ file cục bộ và đối chiếu hình ảnh, không phải mã RGB
được công bố trực tiếp trong mô tả nguồn. Không tự áp dụng cho phiên bản khác.

Lần đánh giá ban đầu bỏ qua màu đen `[0, 0, 0]` như vùng chưa gán nhãn/biên; đây
là lựa chọn đánh giá cần giữ rõ trong manifest, không phải định nghĩa lớp được
nguồn xác nhận. Nhãn RGBA có alpha 0 cũng bị bỏ qua; alpha nằm giữa 0 và 255 bị
từ chối vì không còn rõ màu lớp. Các màu còn lại được xem là không phải nước.

## 2. Chuẩn bị dữ liệu

```powershell
.\.venv\Scripts\python.exe -m evaluation prepare `
  --archive "media/Flood Amateur Video for Semantic Segmentation Dataset.zip" `
  --output data/evaluation/parepare_new `
  --water-rgb 51 221 255 `
  --ignore-rgb 0 0 0
```

Có thể bỏ `--water-rgb` nếu chưa biết bảng màu. Manifest được tạo nhưng đánh giá
sẽ từ chối chạy cho tới khi chỉ định lớp nước. Không lấy dự đoán của model làm
nhãn tham chiếu.

Đầu ra có `images/`, `masks/`, `manifest.json`, `pairing.json`. Mặc định mọi mẫu
đều `unassigned`, không tự gán thành tập test độc lập. `training_overlap` mặc định
là `unknown`; chỉ đổi thành `excluded` khi có bằng chứng rằng dữ liệu đánh giá
không nằm trong tập huấn luyện của checkpoint.

## 3. Định dạng manifest cho dữ liệu khác

Đường dẫn ảnh/nhãn tương đối với vị trí manifest; đường dẫn tuyệt đối cũng được hỗ
trợ. `source_id` nên là video/camera/đợt ghi hình dùng để chống chia nhầm các ảnh
tương tự vào nhiều tập. Không đặt mỗi khung hình một `source_id`.

```json
{
  "version": 1,
  "training_overlap": "unknown",
  "mask_encoding": {
    "kind": "binary",
    "water_values": [255],
    "ignore_values": []
  },
  "samples": [
    {
      "id": "camera_a_0001",
      "image": "images/frame_0001.png",
      "mask": "masks/frame_0001.png",
      "source_id": "camera_a_session_01",
      "split": "test",
      "condition": "night_rain"
    }
  ]
}
```

- `kind=binary`: ảnh nhãn một kênh, chỉ có 0/1 hoặc 0/255. Chọn `water_values`
  đúng theo cách lưu nhãn; `[1,255]` cho phép hai cách lưu ở các file khác nhau.
- `kind=indexed`: ảnh nhãn một kênh, chỉ định chỉ số lớp nước và lớp bỏ qua.
- `kind=rgb`: `water_values`/`ignore_values` là danh sách bộ ba RGB, ví dụ
  `[[51,221,255]]`. So khớp màu chính xác; không dùng JPEG cho nhãn.
- Các `split` hợp lệ: `train`, `validation`, `test`, `unassigned`.
- Ảnh và nhãn phải cùng kích thước. Không tự resize nhãn để che lỗi căn chỉnh.

```powershell
.\.venv\Scripts\python.exe -m evaluation validate `
  --manifest data/evaluation/parepare/manifest.json
```

Kiểm tra tất cả ảnh, nhãn, kích thước, giá trị mã hóa và nguồn bị chia vào nhiều
tập. Kiểm tra hash bắt được ảnh trùng byte ở các tập khác nhau; không phát hiện
được tất cả ảnh gần giống hoặc ảnh đã mã hóa lại. Việc tách nguồn vẫn cần được
kiểm tra về mặt nội dung.

## 4. Chạy đánh giá

Đánh giá thăm dò dataset đang có:

```powershell
.\.venv\Scripts\python.exe -m evaluation evaluate `
  --manifest data/evaluation/parepare/manifest.json `
  --split unassigned `
  --output outputs/riwa_review
```

Chạy nhanh trên 25 mẫu, lấy phân bố theo nguồn và vị trí trong chuỗi:

```powershell
.\.venv\Scripts\python.exe -m evaluation evaluate `
  --manifest data/evaluation/parepare/manifest.json `
  --split unassigned --limit 25 --worst 5 `
  --output outputs/riwa_review_subset
```

Với tập test độc lập đã chuẩn bị, dùng `--split test` (mặc định). Không điều chỉnh
ngưỡng trên tập test rồi dùng lại nó để công bố kết quả cuối. `--backend riwa/yolo`
và `--model <path>` cho phép so sánh checkpoint trên cùng manifest; nếu không chỉ
định thì dùng cấu hình hiện tại trong `.env`.

Mỗi lần chạy cần thư mục đầu ra mới hoặc trống để không ghi đè báo cáo cũ.

Đầu ra:

- `report.html`: báo cáo đọc trong trình duyệt, thống kê theo nguồn và ảnh lỗi.
- `report.json`: số liệu tổng hợp, theo nguồn/điều kiện và từng ảnh; cấu hình
  model, hash model/manifest, cảnh báo về giới hạn đánh giá.
- `samples.csv`: bảng từng ảnh, có thể mở bằng Excel.
- `manifest.snapshot.json`: bản sao manifest dùng ở lần chạy.
- `previews/`: ảnh gốc, nhãn nước tham chiếu và dự đoán của các mẫu IoU thấp.

### Cách đọc số liệu

- `micro`: cộng TP/FP/FN/TN toàn bộ rồi tính chỉ số; ảnh lớn có trọng số cao hơn.
- `macro`: trung bình chỉ số từng ảnh; kèm số mẫu có chỉ số xác định.
- Khi cả nhãn và dự đoán không có nước, IoU/Dice không xác định (`null`), không
  tự chấm là 1. Mẫu này vẫn được tính vào số mẫu âm và số liệu false positive.
- `water_ratio_mae`: sai số tuyệt đối trung bình của tỷ lệ pixel nước, thang 0–1.
  Ví dụ `0.10` tương ứng 10 điểm phần trăm, không phải sai số độ sâu.
- Thời gian suy luận bao gồm tiền xử lý, model, trả kết quả về CPU và lọc mask;
  không bao gồm đọc ảnh, xuất báo cáo hay khung hình warmup.
- `--cpu-threads` mặc định 4, `--warmup` mặc định 1.
- Confidence trung bình của model không phải độ chính xác được kiểm chứng.

## 5. Video chưa có nhãn

```powershell
.\.venv\Scripts\python.exe -m evaluation profile `
  --video "media/Fixed Hikuwai.mp4" `
  --interval-sec 2 --max-frames 12 `
  --output outputs/camera_annotation_batch
```

Xuất ảnh gốc vào `images/`, dự đoán vào `predictions/`, ảnh xem nhanh vào
`previews/`, tỷ lệ nước và thời gian suy luận vào `samples.csv`/`profile.json`.
`masks/` cố ý để trống. Người gán nhãn tạo mặt nạ nhị phân 0/255 ở `masks/` theo
tên file trong `annotation_manifest.json`, rồi dùng manifest này để đánh giá.
Không sao chép `predictions/` sang `masks/` để tự đánh giá chính model đó.

Lệnh này lấy mẫu để đánh giá hiệu năng và chuẩn bị nhãn; không mô phỏng luồng
thời gian của hệ thống cảnh báo và không tạo IoU/Dice khi chưa có nhãn thật.

## 6. Quyết định về model sau khi có báo cáo

1. Xem ảnh lỗi để loại lỗi nhãn, ghép cặp hoặc tiền xử lý.
2. Nếu mask tốt nhưng cảnh báo sai: cải thiện ROI/ngưỡng/logic thời gian.
3. Nếu mask sai lặp lại trong điều kiện mục tiêu: bổ sung nhãn và thử fine-tune.
4. So sánh trên validation, chỉ dùng test độc lập cho kết quả cuối.
5. Bổ sung video không ngập/đường ướt/phản chiếu để đo cảnh báo sai; dữ liệu chỉ
   chứa cảnh ngập không đủ để xác nhận hệ thống vận hành tốt.

Các lệnh trong hướng dẫn này đo segmentation và hiệu năng. Để kiểm tra ROI,
camera, gửi thông báo và chất lượng cảnh báo theo sự kiện, xem
[operations-validation.md](operations-validation.md).
