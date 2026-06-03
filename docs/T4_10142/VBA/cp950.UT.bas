Attribute VB_Name = "UT"

Public Type MAST
    Code As String              ' 商品代碼
    Name As String              ' 商品名稱
    Target As String            ' 商品標的
End Type

' 成交回報與下單回傳值
Public Type ResponseSt
    branch As String            ' 分公司代碼
    account As String           ' 帳戶
    ord_no As String            ' 委託書號
    ord_seq As String           ' 網路單號
    code As String              ' 商品代碼
    place_price As String       ' 委託價
    matched_price As String     ' 成交價
    volume As String            ' 量
    time As String              ' 時間
    status As String            ' 狀態
    err As String               ' 伺服器訊息
    ord_type As String          ' 委託別
    place_type As String        ' 
    ord_knd As String           ' 
                                ' ----
    seqn As String              ' 
    ord_class As String         '
    errno As String             ' 錯誤代碼
    web_id As String            ' 來源別
    account_s As String         '
    oct_type As String          ' 倉別
    ord_time As String          ' 委託時間
    agent_id As String
    price_type As String        ' 價別
    tr_fld As String            '
    matched_seqn As String      ' 成交序號
    func_seqn As String         '
    mprice_flag As String       ' 市價註記
End Type

' 讀取下單回傳電文
Public Function ParseReply(ByVal reply As String, ByRef st As ResponseSt ) As Long
    ParseReply = _
    T4.parse_reply( reply _
    , st.branch _
    , st.account _
    , st.ord_no _
    , st.ord_seq _
    , st.code _
    , st.place_price _
    , st.matched_price _
    , st.volume _
    , st.time _
    , st.status _
    , st.err _
    , st.ord_type _
    , st.place_type _
    , st.ord_knd )
End Function

' 讀取主動回報電文
Public Function ParseResponse(ByVal resp As String, ByRef st As ResponseSt ) As Long
    ParseResponse = _
    T4.parse_response( resp, st.seqn _
    , st.branch, st.account _
    , st.ord_no, st.ord_seq, st.code _
    , st.ord_type _
    , st.ord_class _
    , st.place_price, st.matched_price, st.ord_knd _
    , st.volume, st.time, st.status _
    , st.errno _
    , st.err _
    , st.web_id, st.account_s _
    , st.oct_type, st.ord_time, st.agent_id _
    , st.price_type, st.tr_fld, st.matched_seqn _
    , st.func_seqn, st.mprice_flag )
End Function

' 讀取 eLeader 證券商品描述
Public Function ParseStkMast(ByVal s1 As String, ByRef st As MAST )
    st.Code = Left(s1, 6)
    st.Name = Mid(s1, 7, 6 - (59 - Len(s1)))
    st.Target = RTrim(Right(s1, 6))
End Function

' 讀取 eLeader 期選商品描述
Public Function ParseFutMast(ByVal s1 As String, ByRef st As MAST )
    st.Code = Left(s1, 10)
    st.Name = Mid(s1, 17, 20 - (LenB(StrConv(s1, vbFromUnicode)) - Len(s1)))
    If LenB(StrConv(s1, vbFromUnicode)) > 54 Then st.Target = RTrim(Right(s1, 6)) Else st.Target = ""
End Function

' 讀取 eLeader 選擇權商品描述
Public Function ParseOptMast(ByVal s1 As String, ByRef st As MAST )
    st.Code = Left(s1, 10)
    st.Name = Mid(s1, 19, 20 - (LenB(StrConv(s1, vbFromUnicode)) - Len(s1)))
    st.Target = RTrim(Left(Right(s1, 7), 6))
End Function

