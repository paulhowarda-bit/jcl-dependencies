//CARDLIB  JOB (OPS),'CARD MEMBERS',CLASS=A,MSGCLASS=X
//*
//* Two steps reading two DIFFERENT MEMBERS of the SAME control-card
//* library: one master file sorted two ways, each way parameterised by
//* its own member of PARM.LIB. The member is what a step actually reads
//* and what stage 2 must retrieve, so each is its own artifact row -
//* naming only the step that read it. Keyed on the library alone the
//* pair collapsed into one row: SORTZIP vanished from the manifest and
//* was never fetched, and SORTNAME was credited with a step that never
//* read it.
//*
//BYNAME   EXEC PGM=SORT
//SORTIN   DD  DSN=PROD.CUST.MASTER,DISP=SHR
//SORTOUT  DD  DSN=PROD.CUST.BYNAME,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD  DSN=PARM.LIB(SORTNAME),DISP=SHR
//SYSPRINT DD  SYSOUT=*
//*
//BYZIP    EXEC PGM=SORT
//SORTIN   DD  DSN=PROD.CUST.MASTER,DISP=SHR
//SORTOUT  DD  DSN=PROD.CUST.BYZIP,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD  DSN=PARM.LIB(SORTZIP),DISP=SHR
//SYSPRINT DD  SYSOUT=*
//
